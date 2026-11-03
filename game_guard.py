# -*- coding: utf-8 -*-
"""
game_guard.py —— 全屏应用/游戏免打扰检测

只读前台窗口信息，不注入、不操作任何进程。
"""

from __future__ import annotations

import ctypes

import psutil
import win32api
import win32con
import win32gui
import win32process

# 常见游戏进程（小写）
GAME_PROCESSES = {
    "league of legends.exe",
    "leagueclient.exe",
    "csgo.exe",
    "cs2.exe",
    "valorant.exe",
    "valorant-win64-shipping.exe",
    "dota2.exe",
    "overwatch.exe",
    "overwatch2.exe",
    "pubg.exe",
    "tslgame.exe",
    "gta5.exe",
    "r5apex.exe",
    "eldenring.exe",
    "genshinimpact.exe",
    "yuanshen.exe",
    "starrail.exe",
    "minecraft.windows.exe",
}

# 这些进程即使铺满屏幕也不算游戏/免打扰
SHELL_PROCESSES = {
    "explorer.exe",
    "weixin.exe",
    "wechat.exe",
    "python.exe",
    "pythonw.exe",
    "code.exe",
    "codebuddy cn.exe",
    "codebuddy.exe",
    "idea64.exe",
    "pycharm64.exe",
    "webstorm64.exe",
    "sublime_text.exe",
    "notepad++.exe",
    "devenv.exe",
    "windowsterminal.exe",
    "cmd.exe",
    "powershell.exe",
}


def screen_size() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def foreground_info() -> tuple[int, str, str, tuple[int, int, int, int]] | None:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        name = psutil.Process(pid).name().lower()
    except Exception:
        name = ""
    title = win32gui.GetWindowText(hwnd)
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    return hwnd, name, title, rect


def monitor_rect(hwnd: int | None = None) -> tuple[int, int, int, int]:
    """取窗口所在显示器的工作区/全屏区域；多屏时不再用主屏硬算。"""
    try:
        if hwnd:
            monitor = win32api.MonitorFromWindow(
                hwnd, win32con.MONITOR_DEFAULTTONEAREST
            )
        else:
            monitor = win32api.MonitorFromWindow(
                0, win32con.MONITOR_DEFAULTTOPRIMARY
            )
        info = win32api.GetMonitorInfo(monitor)
        left, top, right, bottom = info["Monitor"]
        return left, top, right, bottom
    except Exception:
        width, height = screen_size()
        return 0, 0, width, height


def is_fullscreen(
    rect: tuple[int, int, int, int], hwnd: int | None = None
) -> bool:
    left, top, right, bottom = rect
    m_left, m_top, m_right, m_bottom = monitor_rect(hwnd)
    mon_width = m_right - m_left
    mon_height = m_bottom - m_top
    if mon_width <= 0 or mon_height <= 0:
        return False
    covers_width = (right - left) >= mon_width * 0.95
    covers_height = (bottom - top) >= mon_height * 0.95
    starts_at_edge = left <= m_left + 2 and top <= m_top + 2
    return covers_width and covers_height and starts_at_edge


def detect() -> tuple[bool, str]:
    """返回 (是否进入免打扰, 原因)。"""
    info = foreground_info()
    if info is None:
        return False, ""
    hwnd, name, title, rect = info

    if name in GAME_PROCESSES:
        return True, f"游戏进程：{name}"

    if name in SHELL_PROCESSES:
        return False, ""

    if is_fullscreen(rect, hwnd):
        return True, f"全屏应用：{name or 'unknown'}"

    return False, ""
