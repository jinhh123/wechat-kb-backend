# -*- coding: utf-8 -*-
"""托盘与状态面板之间的轻量状态/命令总线（JSON 文件，原子写）。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

OUTPUT_DIR = Path(r"D:\KnowledgeBase")
STATUS_FILE = OUTPUT_DIR / ".tray_status.json"
COMMAND_FILE = OUTPUT_DIR / ".tray_command.json"

_LOCK = threading.Lock()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def read_status() -> dict[str, Any]:
    try:
        with open(STATUS_FILE, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_status(**fields: Any) -> dict[str, Any]:
    with _LOCK:
        data = read_status()
        data.update(fields)
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write_json(STATUS_FILE, data)
        return data


def send_command(command: str, **payload: Any) -> None:
    _write_json(
        COMMAND_FILE,
        {
            "command": command,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **payload,
        },
    )


def pop_command() -> dict[str, Any] | None:
    if not COMMAND_FILE.exists():
        return None
    try:
        with open(COMMAND_FILE, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        COMMAND_FILE.unlink()
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        try:
            COMMAND_FILE.unlink()
        except OSError:
            pass
        return None
