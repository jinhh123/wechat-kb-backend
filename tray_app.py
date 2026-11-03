# -*- coding: utf-8 -*-
"""
tray_app.py —— 常驻托盘：空闲扫描 + 全局热键

整合：
    - chat_watcher：空闲时扫描「锦」会话里的「群聊的聊天记录」，自动打开/抓取/关闭；
    - hotkey_capture：Ctrl+Alt+K 采集剪贴板文字/图片；
    - ingest：统一去重、防抖、AI 分类、Markdown 落盘。

托盘菜单：
    暂停/恢复监听、立即扫描 (Ctrl+Alt+S)、查看日志、退出。

全局热键：
    Ctrl+Alt+K  采集剪贴板文字/图片
    Ctrl+Alt+S  忽略空闲，遍历当前窗口全部记录卡片

防抢鼠标：
    - 自动扫描只在系统空闲超过 180 秒时执行；
    - 检测到用户正在操作（空闲 < 5 秒）直接跳过本轮；
    - 「立即扫描」是人工触发，会忽略空闲限制，但仍在后台线程执行。

运行：
    .\\venv\\Scripts\\pythonw.exe tray_app.py          # 无黑框常驻
    .\\venv\\Scripts\\python.exe  tray_app.py --dry-run # 调试，不写文件
    .\\venv\\Scripts\\python.exe  tray_app.py --help
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

import pystray
import win32api
import win32con
import win32event
import winerror
from PIL import Image, ImageDraw

import chat_watcher
import game_guard
from focus_guard import ForegroundGuard
import hotkey_capture
import ingest
import message_watcher
import status_bus

OUTPUT_DIR = ingest.OUTPUT_DIR
LOG_PATH = OUTPUT_DIR / "tray_app.log"

HOTKEY_ID = 0xB002
SCAN_HOTKEY_ID = 0xB003
VK_K = 0x4B
VK_S = 0x53
DEFAULT_IDLE_THRESHOLD = 180
DEFAULT_SCAN_INTERVAL = 60
BUSY_WINDOW_SECONDS = 5.0

MUTEX_NAME = r"Global\WeChatKB_TrayApp_SingleInstance"
_INSTANCE_MUTEX = None

LOG = logging.getLogger("tray_app")


def _ensure_streams() -> None:
    """pythonw.exe 下 sys.stdout/stderr 为 None，print/log 会抛异常，统一兜底。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def setup_logging() -> None:
    if LOG.handlers:
        return
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        LOG.addHandler(console)

    try:
        from logging.handlers import RotatingFileHandler

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
    except OSError:
        pass


# =============================================================================
# 图片原图通道（占位）
# =============================================================================
def capture_image_original(hwnd: int, box: tuple[int, int, int, int]) -> str | None:
    """
    TODO（等托盘跑通后实现）：
        1. 在记录窗口里双击图片，等待图片预览窗口；
        2. 发送 Ctrl+S 或点击「保存」；
        3. 在文件对话框里填入 D:\\KnowledgeBase\\images\\xxx.png；
        4. 回车保存，Esc 关闭预览；
        5. 返回原图路径。

    当前仅预留接口，不执行任何点击。
    """
    LOG.info("图片原图通道尚未实现（hwnd=%s box=%s）", hwnd, box)
    return None


# =============================================================================
# 托盘应用
# =============================================================================
class TrayApp:
    def __init__(self, dry_run: bool = False, idle_threshold: int = DEFAULT_IDLE_THRESHOLD,
                 scan_interval: int = DEFAULT_SCAN_INTERVAL,
                 scroll_history: bool = False, scroll_pages: int = 5) -> None:
        self.dry_run = dry_run
        self.idle_threshold = idle_threshold
        self.scan_interval = scan_interval
        self.scroll_history = scroll_history
        self.scroll_pages = scroll_pages

        self.paused = threading.Event()
        self.stop_event = threading.Event()
        self.ui_lock = threading.Lock()      # 扫描与热键互斥，避免剪贴板冲突
        self.scan_lock = threading.Lock()    # 防止重复扫描
        self.pipeline: ingest.Pipeline | None = None
        self.message_watcher: message_watcher.MessageWatcher | None = None
        self.icon: pystray.Icon | None = None
        self.hotkey_thread: threading.Thread | None = None
        self.hotkey_thread_id: int | None = None
        self.game_mode = False
        self.game_reason = ""
        self._closed = False

    # ------------------------------------------------------------------ 生命周期
    def run(self) -> int:
        setup_logging()
        # 托盘启动即声明 DPI 感知，保证多屏/150% 缩放下坐标一致
        chat_watcher.rc.enable_dpi_awareness()
        self.pipeline = ingest.Pipeline(dry_run=self.dry_run)
        self.message_watcher = message_watcher.MessageWatcher(
            self.pipeline, dry_run=self.dry_run
        )

        status_bus.write_status(
            running=True,
            paused=False,
            scanning=False,
            game_mode=False,
            game_reason="",
            current_chat="待识别",
            hotkeys="Ctrl+Alt+K / Ctrl+Alt+S",
            last_error="",
            today_archived=self._today_count(),
            recent_files=self._recent_files(),
        )

        self.hotkey_thread = threading.Thread(
            target=self._hotkey_loop, name="hotkey", daemon=True
        )
        self.hotkey_thread.start()

        threading.Thread(target=self._scan_loop, name="scanner", daemon=True).start()
        threading.Thread(
            target=self._message_loop, name="messages", daemon=True
        ).start()
        threading.Thread(
            target=self._game_loop, name="game-guard", daemon=True
        ).start()
        threading.Thread(
            target=self._command_loop, name="commands", daemon=True
        ).start()

        self.icon = pystray.Icon(
            "wechat_kb",
            self._icon_image(),
            "微信知识库采集",
            self._menu(),
        )
        LOG.info(
            "托盘已启动：空闲阈值 %d 秒，扫描间隔 %d 秒，dry_run=%s，scroll_history=%s(%d 屏)",
            self.idle_threshold,
            self.scan_interval,
            self.dry_run,
            self.scroll_history,
            self.scroll_pages,
        )
        self.icon.run()
        self._shutdown()
        return 0

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        self._stop_hotkey()
        if self.hotkey_thread is not None:
            self.hotkey_thread.join(timeout=3)
        if self.message_watcher is not None:
            try:
                self.message_watcher.close()
            except Exception:
                LOG.exception("关闭消息聚合失败")
        if self.pipeline is not None:
            try:
                self.pipeline.close()
            except Exception:
                LOG.exception("关闭 ingest 失败")
        status_bus.write_status(running=False, scanning=False, paused=True)
        LOG.info("托盘已退出")

    # ------------------------------------------------------------------ 菜单
    def _menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: "恢复监听" if self.paused.is_set() else "暂停监听",
                self._toggle_pause,
            ),
            pystray.MenuItem("立即扫描 (Ctrl+Alt+S)", self._scan_now),
            pystray.MenuItem("状态面板", self._open_panel),
            pystray.MenuItem("查看日志", self._open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._quit),
        )

    def _icon_image(self) -> Image.Image:
        image = Image.new("RGB", (64, 64), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=16, fill=(7, 193, 96))
        draw.text((16, 21), "KB", fill="white")
        return image

    def _set_paused(self, paused: bool) -> None:
        if paused:
            self.paused.set()
            LOG.info(
                "已暂停监听（Ctrl+Alt+K 与自动扫描停止；"
                "手动「立即扫描」和 Ctrl+Alt+S 仍可用）"
            )
        else:
            self.paused.clear()
            LOG.info("已恢复监听")
        status_bus.write_status(paused=paused)
        if self.icon is not None:
            self.icon.update_menu()

    def _toggle_pause(self, icon: pystray.Icon, item: Any) -> None:
        self._set_paused(not self.paused.is_set())

    def _trigger_scan(self) -> None:
        LOG.info("手动触发扫描：遍历当前可见的全部卡片")
        status_bus.write_status(scanning=True)
        threading.Thread(
            target=self._run_scan,
            kwargs={
                "force": True,
                "all_cards": True,
                "scroll": self.scroll_history,
            },
            name="scan-now",
            daemon=True,
        ).start()

    def _scan_now(self, icon: pystray.Icon, item: Any) -> None:
        self._trigger_scan()

    def _open_panel(self, icon: pystray.Icon, item: Any) -> None:
        # 已经开着就拉到前台，避免重复开窗
        existing: list[int] = []

        def callback(hwnd: int, _extra) -> None:
            if win32gui.IsWindowVisible(hwnd) and "微信知识库采集" in win32gui.GetWindowText(hwnd):
                existing.append(hwnd)

        win32gui.EnumWindows(callback, None)
        if existing:
            try:
                win32gui.SetForegroundWindow(existing[0])
            except Exception:
                pass
            return

        panel = Path(__file__).with_name("status_panel.py")
        try:
            subprocess.Popen(
                [sys.executable, str(panel)],
                cwd=str(Path(__file__).parent),
                close_fds=True,
            )
        except Exception:
            LOG.exception("打开状态面板失败")

    def _today_count(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        count = 0
        try:
            for path in OUTPUT_DIR.glob("*.md"):
                if datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d") == today:
                    count += 1
        except OSError:
            pass
        return count

    def _recent_files(self) -> list[str]:
        try:
            paths = sorted(
                OUTPUT_DIR.glob("*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:8]
            return [p.name for p in paths]
        except OSError:
            return []

    def _command_loop(self) -> None:
        while not self.stop_event.wait(1.0):
            command = status_bus.pop_command()
            if not command:
                continue
            name = command.get("command")
            LOG.info("面板命令：%s", name)
            if name == "scan":
                self._trigger_scan()
            elif name == "toggle_pause":
                self._set_paused(not self.paused.is_set())
            elif name == "quit":
                self._quit(self.icon, None)

    def _open_log(self, icon: pystray.Icon, item: Any) -> None:
        target = chat_watcher.LOG_PATH
        if not target.exists():
            target = OUTPUT_DIR
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception:
            LOG.exception("打开日志失败：%s", target)

    def _quit(self, icon: pystray.Icon, item: Any) -> None:
        LOG.info("收到退出指令")
        self.stop_event.set()
        self._stop_hotkey()
        if icon is not None:
            icon.stop()

    # ------------------------------------------------------------------ 扫描
    def _scan_loop(self) -> None:
        # 启动后先缓 10 秒，避免和登录/开机动作抢鼠标
        if self.stop_event.wait(10):
            return

        scans = 0
        while not self.stop_event.is_set():
            if self.paused.is_set():
                self.stop_event.wait(self.scan_interval)
                continue

            if self.game_mode:
                LOG.info(
                    "全屏/游戏模式（%s），自动记录扫描挂起",
                    self.game_reason or "全屏应用",
                )
                self.stop_event.wait(self.scan_interval)
                continue

            idle = chat_watcher.idle_seconds()
            if idle < BUSY_WINDOW_SECONDS:
                LOG.info("用户正在操作（空闲 %.1f 秒），跳过本轮", idle)
            elif idle < self.idle_threshold:
                LOG.info(
                    "空闲不足 %d 秒（当前 %.0f 秒），本轮不点击鼠标",
                    self.idle_threshold,
                    idle,
                )
            else:
                self._run_scan(force=False)
                scans += 1
                if scans % 10 == 0:
                    gc.collect()

            self.stop_event.wait(self.scan_interval)

    def _game_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                mode, reason = game_guard.detect()
            except Exception:
                LOG.exception("全屏检测失败")
                mode, reason = False, ""

            if mode != self.game_mode or reason != self.game_reason:
                self.game_mode = mode
                self.game_reason = reason
                if mode:
                    LOG.warning("检测到全屏/游戏模式：%s，自动化已挂起", reason)
                else:
                    LOG.info("已退出全屏/游戏模式，自动化恢复")
                status_bus.write_status(
                    game_mode=mode,
                    game_reason=reason,
                )

            self.stop_event.wait(10.0)

    def _message_loop(self) -> None:
        """轻量消息轮询：每 15 秒只做 OCR，不点击、不抢焦点。"""
        if self.stop_event.wait(15):
            return

        while not self.stop_event.is_set():
            if self.paused.is_set() or self.game_mode:
                self.stop_event.wait(15)
                continue

            # 二次确认，避免游戏在 10 秒窗口内刚启动
            try:
                mode, reason = game_guard.detect()
            except Exception:
                mode, reason = False, ""
            if mode:
                if not self.game_mode:
                    self.game_mode = True
                    self.game_reason = reason
                    status_bus.write_status(game_mode=True, game_reason=reason)
                self.stop_event.wait(15)
                continue

            try:
                with self.ui_lock:
                    if self.message_watcher is not None:
                        self.message_watcher.scan_once()
            except Exception:
                LOG.exception("轻量消息轮询失败")
                status_bus.write_status(last_error="轻量消息轮询失败")

            self.stop_event.wait(15)

    def _run_scan(
        self, force: bool, all_cards: bool = False, scroll: bool = False
    ) -> None:
        if self.stop_event.is_set():
            return
        if not self.scan_lock.acquire(blocking=False):
            LOG.info("已有扫描在进行，跳过")
            return

        try:
            with self.ui_lock:
                argv = ["--process"]
                if force:
                    argv.append("--force")
                if all_cards:
                    argv.append("--all")
                if scroll:
                    argv.extend(
                        ["--scroll-history", "--max-scroll-pages", str(self.scroll_pages)]
                    )
                if self.dry_run:
                    argv.append("--dry-run")
                LOG.info("开始扫描：%s", " ".join(argv))
                with ForegroundGuard():
                    code = chat_watcher.run(chat_watcher.parse_args(argv))
                LOG.info("扫描结束，返回 %s", code)
                status_bus.write_status(
                    scanning=False,
                    last_scan_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    last_scan_result=f"返回 {code}",
                    today_archived=self._today_count(),
                    recent_files=self._recent_files(),
                    last_error="",
                )
        except Exception as exc:
            LOG.exception("扫描异常")
            status_bus.write_status(scanning=False, last_error=str(exc))
        finally:
            self.scan_lock.release()

    # ------------------------------------------------------------------ 热键
    def _hotkey_loop(self) -> None:
        user32 = ctypes.windll.user32
        self.hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        modifiers = win32con.MOD_CONTROL | win32con.MOD_ALT
        hotkeys = [
            (HOTKEY_ID, VK_K, "Ctrl+Alt+K（剪贴板采集）"),
            (SCAN_HOTKEY_ID, VK_S, "Ctrl+Alt+S（强制扫描全部卡片）"),
        ]

        registered: list[int] = []
        for hotkey_id, vk, label in hotkeys:
            if user32.RegisterHotKey(None, hotkey_id, modifiers, vk):
                registered.append(hotkey_id)
                LOG.info("热键已注册：%s", label)
            else:
                LOG.error("注册热键失败：%s（可能已被其他程序占用）", label)

        if not registered:
            LOG.error("所有热键都注册失败，热键功能不可用")
            return

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message != win32con.WM_HOTKEY:
                    continue
                if msg.wParam == HOTKEY_ID:
                    self._on_hotkey()
                elif msg.wParam == SCAN_HOTKEY_ID:
                    self._on_scan_hotkey()
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            LOG.info("热键已注销")

    def _on_hotkey(self) -> None:
        if self.paused.is_set():
            LOG.info("已暂停监听，忽略 Ctrl+Alt+K")
            return
        try:
            with self.ui_lock:
                ok = hotkey_capture.handle_clipboard(self.pipeline, self.dry_run)
            LOG.info("热键处理完成：%s", "submitted" if ok else "empty/duplicate")
            status_bus.write_status(
                last_hotkey_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                last_hotkey_result="submitted" if ok else "empty/duplicate",
                today_archived=self._today_count(),
                recent_files=self._recent_files(),
            )
        except Exception as exc:
            LOG.exception("热键处理失败")
            status_bus.write_status(last_error=str(exc))

    def _on_scan_hotkey(self) -> None:
        """Ctrl+Alt+S：忽略空闲检测，遍历当前可见的全部卡片。"""
        LOG.info("收到 Ctrl+Alt+S：强制扫描全部可见卡片（忽略空闲）")
        threading.Thread(
            target=self._run_scan,
            kwargs={
                "force": True,
                "all_cards": True,
                "scroll": self.scroll_history,
            },
            name="scan-hotkey",
            daemon=True,
        ).start()

    def _stop_hotkey(self) -> None:
        if not self.hotkey_thread_id:
            return
        try:
            ctypes.windll.user32.PostThreadMessageW(
                self.hotkey_thread_id, win32con.WM_QUIT, 0, 0
            )
        except Exception:
            LOG.exception("停止热键线程失败")


# =============================================================================
# CLI
# =============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="微信知识库采集托盘程序")
    parser.add_argument("--dry-run", action="store_true", help="不写 Markdown")
    parser.add_argument(
        "--idle-threshold", type=int, default=DEFAULT_IDLE_THRESHOLD,
        help="自动扫描所需空闲秒数（默认 180）",
    )
    parser.add_argument(
        "--scan-interval", type=int, default=DEFAULT_SCAN_INTERVAL,
        help="两次自动扫描间隔秒数（默认 60）",
    )
    parser.add_argument(
        "--scroll-history",
        action="store_true",
        help="手动扫描时向上翻页抓历史卡片（默认关闭）",
    )
    parser.add_argument(
        "--scroll-pages", type=int, default=5, help="手动扫描最多向上翻几屏"
    )
    return parser.parse_args(argv)


def acquire_single_instance():
    """命名互斥：防止开了两个托盘，互相抢 Ctrl+Alt+K。"""
    handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        win32api.CloseHandle(handle)
        return None
    return handle


def main(argv: list[str] | None = None) -> int:
    global _INSTANCE_MUTEX

    _ensure_streams()
    args = parse_args(argv)

    _INSTANCE_MUTEX = acquire_single_instance()
    if _INSTANCE_MUTEX is None:
        setup_logging()
        LOG.error("已有托盘实例在运行，本实例退出（避免热键/扫描冲突）")
        return 0

    app = TrayApp(
        dry_run=args.dry_run,
        idle_threshold=args.idle_threshold,
        scan_interval=args.scan_interval,
        scroll_history=args.scroll_history,
        scroll_pages=args.scroll_pages,
    )
    return app.run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
