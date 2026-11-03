# -*- coding: utf-8 -*-
"""self_check.py —— 微信 UI 兼容性自检。"""

from __future__ import annotations

import asyncio

import win32gui

import chat_watcher
import record_capture as rc


def run() -> tuple[bool, str]:
    main = chat_watcher.find_main_window()
    if main is None:
        return False, "未找到微信主窗口（未启动或微信版本不兼容）"

    hwnd, _title = main
    class_name = win32gui.GetClassName(hwnd)
    if not class_name.startswith("Qt"):
        return False, f"微信窗口类名异常：{class_name!r}（UI 可能已变化）"

    if win32gui.IsIconic(hwnd):
        return True, "微信窗口最小化，等待恢复"

    try:
        image = rc.capture_window(hwnd)
        chat_name = chat_watcher.current_chat_name(image)
        if not chat_name:
            return False, "无法读取聊天标题栏（UI 可能已变化）"
        boxes = asyncio.run(rc.ocr_boxes(image))
        if not boxes:
            return False, "OCR 没有识别到文字（窗口空白或 UI 不兼容）"
        return True, f"微信 UI 正常，当前聊天：{chat_name}"
    except Exception as exc:
        return False, f"微信 UI 自检异常：{exc}"
