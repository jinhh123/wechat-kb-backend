# -*- coding: utf-8 -*-
"""
clipboard_guard.py —— 剪贴板快照/恢复

支持文字和图片（CF_UNICODETEXT / CF_DIB）。
任何抓取动作结束后，尽量把用户原本的剪贴板内容原样写回。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import win32clipboard
import win32con
from PIL import Image, ImageGrab

LOG = logging.getLogger("clipboard_guard")


@dataclass
class ClipboardSnapshot:
    text: str | None = None
    image: Image.Image | None = None


def _read_text() -> str | None:
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return None
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
    except Exception:
        return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
    return None


def _read_image() -> Image.Image | None:
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return None
    return data if isinstance(data, Image.Image) else None


def snapshot() -> ClipboardSnapshot:
    return ClipboardSnapshot(text=_read_text(), image=_read_image())


def _image_to_dib(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="BMP")
    # BMP 文件头 14 字节，CF_DIB 需要从 BITMAPINFOHEADER 开始
    return buffer.getvalue()[14:]


def restore(data: ClipboardSnapshot) -> None:
    if data.text is None and data.image is None:
        return

    try:
        win32clipboard.OpenClipboard()
    except Exception:
        LOG.warning("恢复剪贴板失败：无法打开剪贴板")
        return

    try:
        win32clipboard.EmptyClipboard()
        if data.text is not None:
            win32clipboard.SetClipboardData(
                win32con.CF_UNICODETEXT, data.text
            )
        if data.image is not None:
            win32clipboard.SetClipboardData(
                win32con.CF_DIB, _image_to_dib(data.image)
            )
    except Exception:
        LOG.exception("恢复剪贴板内容失败")
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


class ClipboardGuard:
    """with ClipboardGuard(): ... 退出时恢复进入前的文字/图片。"""

    def __enter__(self) -> "ClipboardGuard":
        self.data = snapshot()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        restore(self.data)
        return False
