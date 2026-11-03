# -*- coding: utf-8 -*-
"""
chat_watcher.py —— 通道 1b：空闲 UI 扫描「锦」会话里的「群聊的聊天记录」

为什么不用系统通知：
    实测 wpndatabase.db 没有微信消息 Handler，WinRT 通知也拿不到任何微信 Toast。

本通道的工作方式：
    1. 仅在系统空闲（默认 3 分钟无键鼠）或 --force 时执行；
    2. 扫描当前聊天窗口，OCR 定位「群聊的聊天记录」卡片；
    3. 双击卡片打开记录窗口；
    4. 调用 record_capture 抓取（剪贴板优先 / OCR 兜底）；
    5. 关闭记录窗口，记录卡片签名，避免重复处理。

前置条件：
    - 微信主窗口已打开，且当前聊天是「锦」（或至少包含要处理的记录卡片）；
    - 运行期间不要抢鼠标；脚本会保存/恢复鼠标位置。

用法：
    .\\venv\\Scripts\\python.exe chat_watcher.py --scan
    .\\venv\\Scripts\\python.exe chat_watcher.py --process --force
    .\\venv\\Scripts\\python.exe chat_watcher.py --process --dry-run --force
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
import win32api
import win32con
import win32gui
import win32process
from PIL import Image, ImageChops

import record_capture as rc

OUTPUT_DIR = Path(r"D:\KnowledgeBase")
SEEN_FILE = OUTPUT_DIR / ".watcher_seen.json"
LOG_PATH = OUTPUT_DIR / "chat_watcher.log"

CARD_TITLE = "群聊的聊天记录"
EXPECTED_CHAT = "锦"
IDLE_THRESHOLD_SECONDS = 180

LOG = logging.getLogger("chat_watcher")


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
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
    except OSError:
        pass


# =============================================================================
# 基础
# =============================================================================
class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    return max(0.0, (tick - info.dwTime) / 1000.0)


def find_main_window() -> tuple[int, str] | None:
    found: list[tuple[int, str]] = []

    def callback(hwnd: int, _extra) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetClassName(hwnd) != "Qt51514QWindowIcon":
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() not in ("weixin.exe", "wechat.exe"):
                return
        except Exception:
            return
        title = win32gui.GetWindowText(hwnd)
        if "聊天记录" in title:
            return
        found.append((hwnd, title))

    win32gui.EnumWindows(callback, None)
    return found[0] if found else None


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sorted(seen), ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        LOG.exception("保存卡片签名失败")


def card_signature(image, box: tuple[float, float, float, float, str]) -> str:
    """
    卡片感知哈希（dHash）。

    滚动后 Qt 的亚像素渲染会让精确 SHA 变化，改用 8x8 相邻像素比较，
    再用海明距离判断“是不是同一张卡”，避免滚动一次就重新打开旧卡片。
    """
    top, left, right, _bottom, _text = box
    crop_left = max(0, int(left) - 10)
    crop_top = max(0, int(top) - 10)
    crop_right = min(image.width, int(right) + 260)
    crop_bottom = min(image.height, int(top) + 160)
    crop = image.crop((crop_left, crop_top, crop_right, crop_bottom))
    gray = crop.convert("L").resize((9, 8), Image.LANCZOS)
    pixels = list(gray.tobytes())

    bits = 0
    for row in range(8):
        for col in range(8):
            left_pixel = pixels[row * 9 + col]
            right_pixel = pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left_pixel > right_pixel else 0)
    return f"{bits:016x}"


def hamming_distance(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 999


def is_signature_seen(signature: str, seen: set[str]) -> bool:
    if signature in seen:
        return True
    # 只和同长度的 dHash 比较；旧版 64 位 SHA 自动忽略
    return any(
        len(item) == 16 and hamming_distance(signature, item) <= 6
        for item in seen
    )


@dataclass
class Card:
    box: tuple[float, float, float, float, str]
    signature: str


def looks_like_card_title(text: str) -> bool:
    """
    单聊记录的标题是「张三和李四的聊天记录」，群聊是「群聊的聊天记录」；
    卡片底部还有一个纯「聊天记录」的按钮，必须排除。
    """
    value = (text or "").strip()
    if value == "聊天记录":
        return False
    return "聊天记录" in value


def current_chat_name(image) -> str:
    """从主窗口顶部右侧读当前聊天标题（微信 4.x 自绘，只能 OCR）。"""
    boxes = asyncio.run(rc.ocr_boxes(image))
    candidates: list[str] = []
    for top, left, _right, _bottom, text in boxes:
        if top > image.height * 0.16:
            continue
        if left < image.width * 0.30:
            continue
        value = (text or "").strip()
        if value:
            candidates.append(value)
    return candidates[0] if candidates else ""


def scan_cards_from_image(image) -> list[Card]:
    boxes = asyncio.run(rc.ocr_boxes(image))
    cards: list[Card] = []

    for box in boxes:
        top, left, _right, _bottom, text = box
        if not looks_like_card_title(text):
            continue
        if left <= image.width * 0.35:  # 只认右侧聊天区
            continue
        cards.append(Card(box=box, signature=card_signature(image, box)))

    cards.sort(key=lambda c: c.box[0])
    return cards


def scan_cards(hwnd: int) -> tuple[Any, list[Card]]:
    image = rc.capture_window(hwnd)
    return image, scan_cards_from_image(image)


def _scroll_message(hwnd: int, delta: int, notches: int) -> None:
    """向聊天区发送滚轮消息：不移动鼠标、不抢焦点。"""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    x = left + int((right - left) * 0.72)
    y = top + int((bottom - top) * 0.55)
    lparam = win32api.MAKELONG(x, y)
    wparam = (delta << 16) & 0xFFFFFFFF
    for _ in range(notches):
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)
        time.sleep(0.08)


def scroll_up_once(hwnd: int, before: Image.Image, notches: int = 4) -> tuple[Image.Image, bool]:
    """向上滚一屏，返回 (截图, 画面是否变化)。变化太小视为已到顶。"""
    _scroll_message(hwnd, 120, notches)
    time.sleep(1.0)
    after = rc.capture_window(hwnd)

    diff = ImageChops.difference(before.convert("L"), after.convert("L"))
    data = diff.tobytes()
    mean = sum(data) / max(1, len(data))
    return after, mean > 0.5


def double_click_card(hwnd: int, image, box: tuple[float, float, float, float, str]) -> None:
    top, left, right, bottom, _text = box
    win_left, win_top, win_right, _win_bottom = win32gui.GetWindowRect(hwnd)
    scale = image.width / max(1, win_right - win_left)

    x = win_left + int((left + right) / 2 * scale)
    y = win_top + int((top + bottom) / 2 * scale)

    old_pos = win32api.GetCursorPos()
    try:
        win32api.SetCursorPos((x, y))
        time.sleep(0.15)
        for _ in range(2):
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.12)
    finally:
        win32api.SetCursorPos(old_pos)


def wait_for_new_record_window(
    existing: set[int], timeout: float = 8.0
) -> int | None:
    """只返回双击之后新出现的记录窗口，绝不盲取列表第一个。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, _title in rc.find_record_windows():
            if hwnd not in existing:
                return hwnd
        time.sleep(0.3)
    return None


def close_record_windows() -> None:
    for hwnd, _title in rc.find_record_windows():
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass


# =============================================================================
# 主流程
# =============================================================================
def run(args: argparse.Namespace) -> int:
    setup_logging()
    rc.enable_dpi_awareness()

    main = find_main_window()
    if main is None:
        LOG.error("没找到微信主窗口")
        return 1

    hwnd, title = main
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(1.0)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.5)

    image, cards = scan_cards(hwnd)
    chat_name = current_chat_name(image)
    if chat_name != EXPECTED_CHAT:
        LOG.error(
            "当前聊天是 %r，不是 %r，已跳过本轮（防止抓错会话）",
            chat_name or "<未识别>",
            EXPECTED_CHAT,
        )
        return 0

    LOG.info(
        "当前聊天：%s，检测到 %d 张聊天记录卡片",
        chat_name,
        len(cards),
    )

    if not cards and not args.scroll_history:
        LOG.warning("没有检测到「群聊的聊天记录」；请先打开「锦」的聊天窗口")
        return 0

    seen = load_seen(Path(args.seen_file) if args.seen_file else SEEN_FILE)
    # --all：本轮遍历当前可见的全部卡片（忽略历史签名，靠 ingest 内容 hash 防重复）
    new_cards = (
        cards
        if args.all
        else [card for card in cards if not is_signature_seen(card.signature, seen)]
    )

    if args.scan or not args.process:
        for index, card in enumerate(cards, start=1):
            flag = "NEW" if card.signature in {c.signature for c in new_cards} else "seen"
            LOG.info(
                "卡片 %d: top=%.0f text=%r signature=%s [%s]",
                index,
                card.box[0],
                card.box[4],
                card.signature[:12],
                flag,
            )
        LOG.info("新模式：--process 才会双击抓取")
        return 0

    if not new_cards and not args.scroll_history:
        LOG.info("没有新卡片")
        return 0

    idle = idle_seconds()
    LOG.info("系统空闲 %.0f 秒（阈值 %d 秒）", idle, args.threshold)
    if idle < args.threshold and not args.force:
        LOG.info("未达到空闲条件，跳过本轮（可加 --force 强制执行）")
        return 0

    processed = 0
    attempts = 0
    processed_signatures: set[str] = set()
    processed_tops: list[float] = []
    scroll_pages = 0
    empty_scrolls = 0
    scrolled = 0

    def pick_pending(cards: list[Card]) -> list[Card]:
        result: list[Card] = []
        for card in cards:
            # 即使 --all，本轮已经处理过的卡也绝不重复双击
            if is_signature_seen(card.signature, processed_signatures):
                continue
            # 顶部坐标太接近，说明是同一张卡（已读后红点消失会导致签名变化）
            if any(abs(card.box[0] - top) <= 40 for top in processed_tops):
                continue
            if not args.all and is_signature_seen(card.signature, seen):
                continue
            result.append(card)
        return result

    # 每处理一张都重新聚焦 + 重新扫描，避免复用上一张的坐标
    while attempts < 200:
        attempts += 1
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.4)

        image, cards = scan_cards(hwnd)
        pending = pick_pending(cards)

        if not pending:
            if not args.scroll_history or scroll_pages >= args.max_scroll_pages:
                break

            before = rc.capture_window(hwnd)
            after, changed = scroll_up_once(hwnd, before)
            scrolled += 1
            scroll_pages += 1
            if not changed:
                LOG.info("第 %d 次向上滚动画面无变化，可能已到顶", scroll_pages)
                break

            LOG.info("向上滚动第 %d 屏，继续查找历史卡片", scroll_pages)
            processed_tops.clear()
            cards = scan_cards_from_image(after)
            pending = pick_pending(cards)

            if not pending:
                empty_scrolls += 1
                if empty_scrolls >= 2:
                    LOG.info("连续 2 屏没有新卡片，停止上翻")
                    break
                continue

            empty_scrolls = 0
            continue

        card = pending[0]
        processed_signatures.add(card.signature)
        processed_tops.append(card.box[0])
        LOG.info("处理卡片：top=%.0f", card.box[0])

        # 先清掉所有遗留记录窗口，避免抓到旧窗口
        close_record_windows()
        time.sleep(0.4)
        existing = {h for h, _t in rc.find_record_windows()}

        double_click_card(hwnd, image, card.box)
        record_hwnd = wait_for_new_record_window(existing)
        if record_hwnd is None:
            LOG.error("双击后没有出现新的记录窗口，标记跳过")
            seen.add(card.signature)
            continue

        try:
            argv = [
                "--ingest",
                "--record-hwnd",
                hex(record_hwnd),
            ] + (["--dry-run"] if args.dry_run else [])
            code = rc.main(argv)
            LOG.info("record_capture 返回：%s", code)
            seen.add(card.signature)
            if code == 0:
                processed += 1
        except Exception:
            LOG.exception("调用 record_capture 失败")
            seen.add(card.signature)
        finally:
            time.sleep(0.5)
            close_record_windows()
            time.sleep(0.5)

    if scrolled:
        _scroll_message(hwnd, -120, 4 * scrolled)
        time.sleep(0.8)
        LOG.info("已向下滚回 %d 屏", scrolled)

    save_seen(Path(args.seen_file) if args.seen_file else SEEN_FILE, seen)
    LOG.info("本轮完成：处理 %d 张，累计签名 %d 个", processed, len(seen))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="空闲扫描「锦」会话里的群聊聊天记录并抓取"
    )
    parser.add_argument("--scan", action="store_true", help="只扫描并列出卡片")
    parser.add_argument("--process", action="store_true", help="扫描并处理新卡片")
    parser.add_argument(
        "--all", action="store_true", help="遍历当前可见的全部卡片（忽略已处理签名）"
    )
    parser.add_argument("--force", action="store_true", help="忽略空闲阈值")
    parser.add_argument(
        "--scroll-history",
        action="store_true",
        help="处理完当前屏后向上滚动，继续抓历史卡片（仅处理未见过的）",
    )
    parser.add_argument(
        "--max-scroll-pages", type=int, default=10, help="最多向上滚几屏"
    )
    parser.add_argument(
        "--threshold", type=int, default=IDLE_THRESHOLD_SECONDS, help="空闲秒数阈值"
    )
    parser.add_argument("--dry-run", action="store_true", help="record_capture 只读不写")
    parser.add_argument("--seen-file", default="", help="自定义已处理签名文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except KeyboardInterrupt:
        LOG.info("收到 Ctrl+C")
        return 0


if __name__ == "__main__":
    sys.exit(main())
