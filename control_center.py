# -*- coding: utf-8 -*-
"""
control_center.py —— 微信知识库管家（应用式桌面控制台）

一个 Tkinter 原生窗口，把当前所有可视化能力收在一起：
    - 一键启动：本地 API + 托盘 + Vue 前端
    - 打开 Vue 控制台 / 知识库
    - 立即扫描、暂停/恢复、状态面板
    - 打开日志、知识库目录
    - 实时状态：托盘/API/前端、当前聊天、队列、倒计时、自检

这个窗口不执行微信 UI 操作，只通过 status_bus 和 HTTP 调用现成服务。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path

import status_bus

ROOT = Path(__file__).resolve().parent
PYTHONW = ROOT / "venv" / "Scripts" / "pythonw.exe"
FRONTEND_DIR = Path(
    os.environ.get("KB_FRONTEND_DIR", r"C:\dev\notice-to-calendar")
)
FRONTEND_DIST = Path(
    os.environ.get("KB_FRONTEND_DIST", str(FRONTEND_DIR / "dist"))
)
API_BASE = "http://127.0.0.1:8000"
FRONTEND_URL = API_BASE + "/console"
KNOWLEDGE_URL = API_BASE + "/knowledge"
ICON_PATH = ROOT / "app.ico"

BG = "#f3f6fb"
CARD = "#ffffff"
TEXT = "#1f2328"
MUTED = "#6b7280"
BLUE = "#1677ff"
GREEN = "#12a150"
ORANGE = "#d97706"
RED = "#dc2626"
BORDER = "#e5eaf1"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def _port_open(port: int, timeout: float = 0.25) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(path: str, timeout: float = 2.0):
    with urllib.request.urlopen(API_BASE + path, timeout=timeout) as resp:
        import json

        return json.loads(resp.read().decode("utf-8"))


def _api_online() -> bool:
    try:
        _http_json("/api/health")
        return True
    except Exception:
        return False


def _spawn(args: list[str], cwd: Path) -> None:
    subprocess.Popen(
        args,
        cwd=str(cwd),
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait(predicate, timeout: float = 10.0, interval: float = 0.35) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _read_status() -> dict:
    if _api_online():
        try:
            return _http_json("/api/status")
        except Exception:
            pass
    data = status_bus.read_status() or {}
    data = dict(data)
    data["api_online"] = _api_online()
    data["frontend_online"] = _port_open(5173)
    data["tray_online"] = bool(data.get("running"))
    return data


def ensure_api() -> str:
    if _api_online():
        return "API 已在运行"
    _spawn([str(PYTHONW), str(ROOT / "api_server.py")], ROOT)
    if _wait(_api_online, timeout=12):
        return "API 已启动"
    return "API 启动失败，请查看 api_server 日志"


def ensure_tray() -> str:
    data = status_bus.read_status() or {}
    if data.get("running") and data.get("updated_at"):
        try:
            from datetime import datetime

            stamp = datetime.strptime(data["updated_at"], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - stamp).total_seconds() <= 8:
                return "托盘已在运行"
        except Exception:
            pass
    _spawn(
        [
            str(PYTHONW),
            str(ROOT / "tray_app.py"),
            "--scroll-history",
            "--scroll-pages",
            "5",
        ],
        ROOT,
    )
    if _wait(
        lambda: bool((status_bus.read_status() or {}).get("running")),
        timeout=12,
    ):
        return "托盘已启动"
    return "托盘启动失败，请查看 tray_app.log"


def ensure_frontend() -> str:
    # 优先用 API 直接托管的 Vue 构建产物，零 npm 依赖、启动最快
    if FRONTEND_DIST.is_dir():
        msg = ensure_api()
        return f"{msg}；前端由本地 API 提供"
    if _port_open(5173):
        return "Vite 前端已在运行"
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        return "未找到 npm，且 dist 构建产物不存在"
    command = f'"{npm}" run dev -- --host 127.0.0.1 --port 5173'
    _spawn(["cmd.exe", "/c", command], FRONTEND_DIR)
    if _wait(lambda: _port_open(5173), timeout=20):
        return "Vite 前端已启动"
    return "前端启动失败，请在 notice-to-calendar 目录检查 npm 输出"


def ensure_all() -> str:
    messages = [ensure_api(), ensure_tray(), ensure_frontend()]
    return "；".join(messages)


def create_shortcut() -> str:
    """在桌面创建带图标的快捷方式。"""
    try:
        from PIL import Image, ImageDraw, ImageFont

        if not ICON_PATH.exists():
            image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            for y in range(256):
                color = (22 + int(y * 0.08), 119, 255)
                draw.line((0, y, 256, y), fill=color)
            draw.rounded_rectangle((18, 18, 238, 238), radius=48, outline="white", width=8)
            try:
                font = ImageFont.truetype("msyhbd.ttc", 104)
            except Exception:
                font = ImageFont.load_default()
            draw.text((128, 128), "KB", fill="white", font=font, anchor="mm")
            image.save(ICON_PATH, sizes=[(256, 256), (64, 64), (32, 32)])
    except Exception:
        pass

    import win32com.client

    desktop = Path.home() / "Desktop"
    link = desktop / "微信知识库管家.lnk"
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(link))
    shortcut.TargetPath = str(PYTHONW)
    shortcut.Arguments = f'"{ROOT / "control_center.py"}"'
    shortcut.WorkingDirectory = str(ROOT)
    if ICON_PATH.exists():
        shortcut.IconLocation = str(ICON_PATH)
    shortcut.Description = "微信本地知识库采集控制台"
    shortcut.Save()
    return str(link)


class ControlCenter:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.busy = False
        self.last_message = ""
        self._closed = False

        self.root.title("微信知识库管家")
        self.root.geometry("940x680")
        self.root.minsize(880, 650)
        self.root.configure(bg=BG)
        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(str(ICON_PATH))
            except Exception:
                pass

        self.service_vars = {
            "tray": tk.StringVar(value="● 托盘检测中"),
            "api": tk.StringVar(value="● API 检测中"),
            "front": tk.StringVar(value="● 前端检测中"),
        }
        self.hero_var = tk.StringVar(value="正在读取状态…")
        self.hero_detail = tk.StringVar(value="")
        self.message_var = tk.StringVar(value="")
        self.queue_var = tk.StringVar(value="0")
        self.today_var = tk.StringVar(value="0")
        self.chat_var = tk.StringVar(value="未识别")
        self.countdown_var = tk.StringVar(value="--")
        self.strategy_var = tk.StringVar(value="正常")
        self.selfcheck_var = tk.StringVar(value="检查中")
        self.recent_var = tk.StringVar(value="暂无归档")

        self._build()
        self._poll()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        # 打开管家时自动确保 API + 托盘在跑；前端按需点按钮启动
        self.root.after(800, self._auto_start)

    # ------------------------------------------------------------------ UI
    def _button(self, parent, text, command, primary=False):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=BLUE if primary else CARD,
            fg="white" if primary else TEXT,
            activebackground="#0f5fd6" if primary else "#eef4ff",
            activeforeground="white" if primary else TEXT,
            relief="solid" if not primary else "flat",
            bd=1 if not primary else 0,
            padx=14,
            pady=6,
            font=("Microsoft YaHei UI", 9, "bold" if primary else "normal"),
            cursor="hand2",
        )

    def _card(self, parent):
        frame = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        return frame

    def _build(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=20, pady=(14, 6))
        tk.Label(
            header, text="微信知识库管家", bg=BG, fg=TEXT,
            font=("Microsoft YaHei UI", 18, "bold"),
        ).pack(side="left")
        tk.Label(
            header, text="本地只读 · 托盘常驻", bg=BG, fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(10, 0), pady=(8, 0))

        services = tk.Frame(header, bg=BG)
        services.pack(side="right")
        for key in ("tray", "api", "front"):
            tk.Label(
                services, textvariable=self.service_vars[key], bg=BG, fg=MUTED,
                font=("Microsoft YaHei UI", 9, "bold"), padx=6,
            ).pack(side="left")

        hero = self._card(self.root)
        hero.pack(fill="x", padx=20, pady=6)
        left = tk.Frame(hero, bg=CARD)
        left.pack(side="left", fill="x", expand=True, padx=16, pady=12)
        tk.Label(
            left, textvariable=self.hero_var, bg=CARD, fg=TEXT,
            font=("Microsoft YaHei UI", 20, "bold"), anchor="w",
        ).pack(fill="x")
        tk.Label(
            left, textvariable=self.hero_detail, bg=CARD, fg=MUTED,
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
        ).pack(fill="x", pady=(4, 0))

        right = tk.Frame(hero, bg=CARD)
        right.pack(side="right", padx=16, pady=12)
        self._button(right, "一键启动全部", self._start_all, primary=True).pack(
            side="left", padx=(0, 8)
        )
        self._button(right, "刷新", self._poll).pack(side="left")

        metrics = tk.Frame(self.root, bg=BG)
        metrics.pack(fill="x", padx=20, pady=3)
        for title, var, color in (
            ("队列深度", self.queue_var, BLUE),
            ("今日归档", self.today_var, GREEN),
            ("当前聊天", self.chat_var, ORANGE),
            ("下次任务", self.countdown_var, "#7c3aed"),
        ):
            box = self._card(metrics)
            box.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(box, text=title, bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(
                anchor="w", padx=12, pady=(9, 0)
            )
            tk.Label(box, textvariable=var, bg=CARD, fg=color, font=("Microsoft YaHei UI", 14, "bold")).pack(
                anchor="w", padx=12, pady=(0, 9)
            )

        grid = tk.Frame(self.root, bg=BG)
        grid.pack(fill="x", padx=20, pady=6)
        cards = [
            ("🌐", "打开知识库前端", "启动 Vite 开发服务器，并在浏览器打开控制台。", "打开前端", self._open_frontend),
            ("📚", "浏览知识库", "打开 /knowledge，查看已归档的日程与知识。", "进入知识库", self._open_knowledge),
            ("🧭", "状态面板", "打开 Tkinter 原生状态窗口，查看队列和自检。", "打开面板", self._open_panel),
            ("🔍", "立即扫描", "忽略空闲与免打扰，处理当前「锦」会话。", "开始扫描", self._scan),
            ("⏸️", "暂停 / 恢复", "暂停自动扫描和剪贴板热键。", "切换", self._toggle),
            ("📄", "日志与目录", "打开运行日志或 D:\\KnowledgeBase。", "打开日志", self._open_log),
        ]
        for index, (icon, title, desc, label, command) in enumerate(cards):
            row, col = divmod(index, 3)
            card = self._card(grid)
            card.grid(row=row, column=col, sticky="nsew", padx=(0, 8), pady=(0, 8))
            grid.grid_columnconfigure(col, weight=1)
            grid.grid_rowconfigure(row, weight=1)
            inner = tk.Frame(card, bg=CARD)
            inner.pack(fill="both", expand=True, padx=12, pady=8)
            tk.Label(inner, text=icon, bg=CARD, font=("Segoe UI Emoji", 18)).pack(anchor="w")
            tk.Label(
                inner, text=title, bg=CARD, fg=TEXT,
                font=("Microsoft YaHei UI", 10, "bold"), anchor="w",
            ).pack(fill="x", pady=(2, 0))
            tk.Label(
                inner, text=desc, bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 8),
                anchor="w", justify="left", wraplength=220,
            ).pack(fill="x", pady=(2, 5))
            btn_row = tk.Frame(inner, bg=CARD)
            btn_row.pack(anchor="w")
            self._button(btn_row, label, command).pack(side="left")
            if title == "日志与目录":
                self._button(btn_row, "目录", self._open_folder).pack(
                    side="left", padx=(6, 0)
                )

        detail = self._card(self.root)
        detail.pack(side="bottom", fill="x", padx=20, pady=(0, 4))
        info = tk.Frame(detail, bg=CARD)
        info.pack(fill="x", padx=14, pady=8)
        for label, var in (
            ("自动策略", self.strategy_var),
            ("微信自检", self.selfcheck_var),
            ("最近归档", self.recent_var),
        ):
            row = tk.Frame(info, bg=CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=CARD, fg=MUTED, width=10, anchor="w", font=("Microsoft YaHei UI", 8)).pack(side="left")
            tk.Label(row, textvariable=var, bg=CARD, fg=TEXT, anchor="w", font=("Microsoft YaHei UI", 9)).pack(side="left", fill="x", expand=True)



    # ------------------------------------------------------------------ 状态
    def _poll(self):
        if self._closed:
            return
        threading.Thread(target=self._poll_worker, daemon=True).start()
        self.root.after(2000, self._poll)

    def _poll_worker(self):
        try:
            data = _read_status()
        except Exception as exc:
            data = {"last_error": str(exc), "api_online": False, "tray_online": False, "frontend_online": False}
        self.root.after(0, self._apply, data)

    def _apply(self, data: dict):
        if self._closed:
            return
        tray = bool(data.get("tray_online"))
        api = bool(data.get("api_online"))
        front = bool(data.get("frontend_online"))
        self.service_vars["tray"].set("● 托盘在线" if tray else "● 托盘离线")
        self.service_vars["api"].set("● API 在线" if api else "● API 离线")
        self.service_vars["front"].set("● 前端运行" if front else "● 前端未开")

        if api and tray:
            self.hero_var.set("采集器运行中")
        elif api:
            self.hero_var.set("API 已启动，托盘未运行")
        else:
            self.hero_var.set("本地服务未启动")
        self.hero_detail.set(
            f"当前聊天：{data.get('current_chat') or '未识别'}   "
            f"最后心跳：{data.get('updated_at') or '暂无'}"
        )

        self.queue_var.set(str(int(data.get("queue_depth") or 0)))
        self.today_var.set(str(int(data.get("today_archived") or 0)))
        self.chat_var.set(str(data.get("current_chat") or "未识别"))
        now = time.time()
        def remain(key):
            try:
                value = float(data.get(key) or 0)
            except (TypeError, ValueError):
                return "--"
            return f"{max(0, round(value - now))}s" if value else "--"
        self.countdown_var.set(
            f"记录 {remain('next_scan_epoch')} / 消息 {remain('next_message_epoch')}"
        )
        if data.get("game_mode"):
            self.strategy_var.set(f"全屏/游戏：{data.get('game_reason') or '已挂起'}")
        elif data.get("auto_pause_reason"):
            self.strategy_var.set(str(data.get("auto_pause_reason")))
        elif data.get("paused"):
            self.strategy_var.set("手动暂停")
        else:
            self.strategy_var.set("正常自动运行")

        if data.get("self_check_error"):
            self.selfcheck_var.set(f"⚠ {data.get('self_check_error')}")
        else:
            self.selfcheck_var.set("正常" if data.get("self_check_ok") else "检查中")
        recent = data.get("recent_files") or []
        self.recent_var.set(" · ".join(str(x) for x in recent[:3]) if recent else "暂无归档")

    # ------------------------------------------------------------------ 行为
    def _run_bg(self, fn, label: str):
        if self.busy:
            return
        self.busy = True
        self.message_var.set(f"{label}…")
        def worker():
            try:
                result = fn()
            except Exception as exc:
                result = f"{label}失败：{exc}"
            self.root.after(0, lambda: self._done(result))
        threading.Thread(target=worker, daemon=True).start()

    def _done(self, result: str):
        self.busy = False
        self.message_var.set(result)
        self.hero_detail.set(result)
        self._poll_worker()

    def _auto_start(self):
        if self._closed:
            return
        self._run_bg(
            lambda: "；".join([ensure_api(), ensure_tray()]),
            "自动启动本地服务",
        )

    def _start_all(self):
        self._run_bg(ensure_all, "启动本地服务")

    def _open_frontend(self):
        def action():
            result = ensure_frontend()
            url = FRONTEND_URL if FRONTEND_DIST.is_dir() else "http://127.0.0.1:5173/console"
            webbrowser.open(url)
            return f"{result}，已打开控制台"
        self._run_bg(action, "打开前端")

    def _open_knowledge(self):
        def action():
            result = ensure_frontend()
            url = KNOWLEDGE_URL if FRONTEND_DIST.is_dir() else "http://127.0.0.1:5173/knowledge"
            webbrowser.open(url)
            return f"{result}，已打开知识库"
        self._run_bg(action, "打开知识库")

    def _open_panel(self):
        panel = ROOT / "status_panel.py"
        _spawn([str(PYTHONW), str(panel)], ROOT)
        self.message_var.set("已打开状态面板")

    def _scan(self):
        def action():
            msg = ensure_tray()
            status_bus.send_command("scan")
            return f"{msg}；已发送扫描命令"
        self._run_bg(action, "触发扫描")

    def _toggle(self):
        def action():
            msg = ensure_tray()
            status_bus.send_command("toggle_pause")
            return f"{msg}；已切换暂停/恢复"
        self._run_bg(action, "切换监听")

    def _open_log(self):
        path = Path(r"D:\KnowledgeBase\tray_app.log")
        if not path.exists():
            path = Path(r"D:\KnowledgeBase")
        os.startfile(str(path))  # type: ignore[attr-defined]
        self.message_var.set(f"已打开：{path}")

    def _open_folder(self):
        os.startfile(r"D:\KnowledgeBase")  # type: ignore[attr-defined]
        self.message_var.set("已打开知识库目录")

    def _close(self):
        self._closed = True
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    if argv and "--install-shortcut" in argv:
        print(create_shortcut())
        return 0
    if not ICON_PATH.exists():
        try:
            create_shortcut()
        except Exception:
            pass
    root = tk.Tk()
    ControlCenter(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
