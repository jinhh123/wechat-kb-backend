# -*- coding: utf-8 -*-
"""runtime_policy.py —— 夜间/电池自动暂停策略。"""

from __future__ import annotations

from datetime import datetime

import psutil

NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 7


def is_night(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return now.hour >= NIGHT_START_HOUR or now.hour < NIGHT_END_HOUR


def battery_pause_reason() -> str:
    try:
        battery = psutil.sensors_battery()
    except Exception:
        return ""
    if battery is None:
        return ""
    if battery.power_plugged is False:
        return f"电池供电（{int(battery.percent)}%）"
    return ""


def auto_pause_reason(now: datetime | None = None) -> str:
    """返回自动暂停原因；空字符串表示可以自动运行。"""
    if is_night(now):
        return "夜间 23:00-07:00"
    return battery_pause_reason()
