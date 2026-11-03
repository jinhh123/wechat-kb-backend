# -*- coding: utf-8 -*-
"""
hotkey_capture.py —— 通道 2：全局热键剪贴板采集（Ctrl+Alt+K）

用途：
    遇到合并转发、长文、带图消息时，手动全选复制，按 Ctrl+Alt+K：
      - 剪贴板有文字 -> 交给 ingest 分类落盘；
      - 剪贴板是图片（或图片文件） -> 保存到 images/ 并随正文归档；
      - 两者都有 -> 一起提交。

为什么不用 CF_HTML：
    实测微信复制只写 CF_UNICODETEXT / CF_TEXT，没有 CF_HTML。
    所以这里拿纯文本 + CF_DIB 图片，已是微信能给的全部。

用法：
    .\\venv\\Scripts\\python.exe hotkey_capture.py          # 常驻，等待 Ctrl+Alt+K
    .\\venv\\Scripts\\python.exe hotkey_capture.py --once    # 立即处理当前剪贴板
    .\\venv\\Scripts\\python.exe hotkey_capture.py --once --dry-run
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import win32clipboard
import win32con
from PIL import Image, ImageGrab

import ingest

SOURCE = "锦"
HOTKEY_ID = 0xB001
VK_K = 0x4B

LOG = logging.getLogger("hotkey_capture")


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

        ingest.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            ingest.OUTPUT_DIR / "hotkey_capture.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
    except OSError:
        pass


# =============================================================================
# 剪贴板
# =============================================================================
def read_clipboard_text() -> str:
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return ""
    try:
        return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
    except Exception:
        return ""
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def grab_clipboard_image():
    """返回 PIL Image 或 None；文件列表（CF_HDROP）只取第一张图片。"""
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        LOG.exception("读取剪贴板图片失败")
        return None

    if data is None:
        return None
    if isinstance(data, list):
        for item in data:
            path = Path(item)
            if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"):
                try:
                    return Image.open(path)
                except Exception:
                    continue
        return None
    return data


def save_clipboard_image(image, dry_run: bool) -> str | None:
    if dry_run:
        LOG.info("[DRY-RUN] 剪贴板图片 %s，不写盘", image.size)
        return None

    ingest.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"clip-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    path = ingest.IMAGES_DIR / name
    image.save(path, format="PNG")
    return str(path)


# =============================================================================
# 处理
# =============================================================================
def handle_clipboard(pipeline: ingest.Pipeline, dry_run: bool) -> bool:
    text = read_clipboard_text().strip()
    image = grab_clipboard_image()

    attachments: list[str] = []
    if image is not None:
        saved = save_clipboard_image(image, dry_run)
        if saved:
            attachments.append(saved)
        if not text:
            text = "[剪贴板图片]"

    if not text:
        LOG.warning("剪贴板里没有文字或图片")
        return False

    LOG.info(
        "捕获剪贴板：%d 字，%d 张图，预览=%r",
        len(text),
        len(attachments),
        text[:60],
    )
    return pipeline.submit(
        ingest.IngestItem(
            channel="hotkey",
            source=SOURCE,
            text=text,
            attachments=attachments,
            immediate=True,
        )
    )


# =============================================================================
# 热键
# =============================================================================
def run_loop(pipeline: ingest.Pipeline, dry_run: bool) -> int:
    user32 = ctypes.windll.user32
    modifiers = win32con.MOD_CONTROL | win32con.MOD_ALT

    if not user32.RegisterHotKey(None, HOTKEY_ID, modifiers, VK_K):
        LOG.error("注册热键 Ctrl+Alt+K 失败（可能已被其他程序占用）")
        return 1

    LOG.info("已注册 Ctrl+Alt+K，等待剪贴板内容…（Ctrl+C 退出）")

    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == win32con.WM_HOTKEY and msg.wParam == HOTKEY_ID:
                try:
                    handle_clipboard(pipeline, dry_run)
                except Exception:
                    LOG.exception("处理剪贴板失败")
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ctrl+Alt+K 剪贴板采集")
    parser.add_argument("--once", action="store_true", help="立即处理一次剪贴板")
    parser.add_argument("--dry-run", action="store_true", help="不写 Markdown")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()

    pipeline = ingest.Pipeline(dry_run=args.dry_run)
    try:
        if args.once:
            ok = handle_clipboard(pipeline, args.dry_run)
            print("submitted" if ok else "empty/duplicate")
            return 0 if ok else 1
        return run_loop(pipeline, args.dry_run)
    finally:
        pipeline.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        LOG.info("收到 Ctrl+C")
        sys.exit(0)
