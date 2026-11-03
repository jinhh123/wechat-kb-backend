# -*- coding: utf-8 -*-
"""
start_services.py —— 静默启动本地 API + 托盘

由 start_tray.vbs 用 pythonw.exe 调用，不创建控制台窗口。
重复执行是安全的：
    - API 已在监听 8000 就直接跳过；
    - 托盘有单实例互斥，重复启动会自己退出。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = Path(r"D:\KnowledgeBase\launcher.log")


def log(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n"
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def main() -> int:
    try:
        import control_center

        api = control_center.ensure_api()
        tray = control_center.ensure_tray()
        log(f"api={api}; tray={tray}")
        return 0
    except Exception as exc:
        log(f"启动失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
