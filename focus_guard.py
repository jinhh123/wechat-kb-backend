# -*- coding: utf-8 -*-
"""focus_guard.py —— 记录并归还原前台窗口。"""

from __future__ import annotations

import logging

import win32con
import win32gui

LOG = logging.getLogger("focus_guard")


def restore_window(target: int) -> None:
    if not target or not win32gui.IsWindow(target):
        return
    if win32gui.GetForegroundWindow() == target:
        return
    try:
        if win32gui.IsIconic(target):
            win32gui.ShowWindow(target, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(target)
    except Exception as exc:
        LOG.warning("归还原前台窗口失败：%s", exc)


class ForegroundGuard:
    """with ForegroundGuard(): 结束后把焦点还给进入前的窗口。"""

    def __enter__(self) -> "ForegroundGuard":
        self.hwnd = win32gui.GetForegroundWindow()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        restore_window(getattr(self, "hwnd", 0))
        return False
