# -*- coding: utf-8 -*-
"""
status_panel.py —— 托盘状态面板（Tkinter 原生小窗）

从托盘菜单打开；默认不置顶，可勾选“置顶”。
只读 status_bus 的 JSON 状态，按钮通过命令文件控制托盘。
"""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk
from pathlib import Path

import status_bus

LOG_PATH = Path(r"D:\KnowledgeBase\chat_watcher.log")
TRAY_LOG = Path(r"D:\KnowledgeBase\tray_app.log")
OUTPUT_DIR = status_bus.OUTPUT_DIR

COLOR_BG = "#f6f8fa"
COLOR_CARD = "#ffffff"
COLOR_TEXT = "#1f2328"
COLOR_MUTED = "#6b7280"
COLOR_GREEN = "#0f9d58"
COLOR_ORANGE = "#d97706"
COLOR_RED = "#dc2626"


def _fmt(value, default="-"):
    if value is None or value == "":
        return default
    return str(value)


class StatusPanel:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("微信知识库采集 · 状态")
        self.root.geometry("540x820")
        self.root.minsize(520, 760)
        self.root.configure(bg=COLOR_BG)

        self.topmost_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="读取中…")
        self.game_var = tk.StringVar(value="🎮 全屏应用：否")
        self.chat_var = tk.StringVar(value="-")
        self.scan_var = tk.StringVar(value="-")
        self.message_var = tk.StringVar(value="-")
        self.queue_var = tk.StringVar(value="0 条")
        self.countdown_var = tk.StringVar(value="-")
        self.self_check_var = tk.StringVar(value="微信 UI：未检查")
        self.today_var = tk.StringVar(value="0")
        self.hotkey_var = tk.StringVar(value="-")
        self.updated_var = tk.StringVar(value="-")
        self.error_var = tk.StringVar(value="")

        self._build()
        self.refresh()
        self._tick()

    # ------------------------------------------------------------------ UI
    def _card(self, parent: tk.Widget, title: str) -> tk.Frame:
        frame = tk.Frame(parent, bg=COLOR_CARD, bd=0, highlightthickness=1)
        frame.configure(highlightbackground="#e5e7eb")
        tk.Label(
            frame,
            text=title,
            bg=COLOR_CARD,
            fg=COLOR_MUTED,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(10, 2))
        return frame

    def _line(self, parent: tk.Widget, label: str, var: tk.StringVar) -> None:
        row = tk.Frame(parent, bg=COLOR_CARD)
        row.pack(fill="x", padx=14, pady=3)
        tk.Label(
            row,
            text=label,
            width=12,
            anchor="w",
            bg=COLOR_CARD,
            fg=COLOR_MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left")
        tk.Label(
            row,
            textvariable=var,
            anchor="w",
            justify="left",
            bg=COLOR_CARD,
            fg=COLOR_TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
            wraplength=360,
        ).pack(side="left", fill="x", expand=True)

    def _build(self) -> None:
        header = tk.Frame(self.root, bg=COLOR_BG)
        header.pack(fill="x", padx=18, pady=(16, 8))
        tk.Label(
            header,
            text="微信知识库采集",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text="本地 · 只读",
            bg=COLOR_BG,
            fg=COLOR_MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(8, 0), pady=(5, 0))

        # 运行状态
        status_card = self._card(self.root, "运行状态")
        status_card.pack(fill="x", padx=16, pady=6)
        self.status_label = tk.Label(
            status_card,
            textvariable=self.status_var,
            bg=COLOR_CARD,
            fg=COLOR_GREEN,
            font=("Microsoft YaHei UI", 20, "bold"),
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=14, pady=(0, 2))
        self.game_label = tk.Label(
            status_card,
            textvariable=self.game_var,
            bg=COLOR_CARD,
            fg=COLOR_GREEN,
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
            wraplength=460,
            justify="left",
        )
        self.game_label.pack(fill="x", padx=14, pady=(0, 6))
        self._line(status_card, "当前聊天", self.chat_var)
        self._line(status_card, "上次记录扫描", self.scan_var)
        self._line(status_card, "上次消息扫描", self.message_var)
        self._line(status_card, "队列深度", self.queue_var)
        self._line(status_card, "下次任务", self.countdown_var)
        self.self_check_label = tk.Label(
            status_card,
            textvariable=self.self_check_var,
            bg=COLOR_CARD,
            fg=COLOR_RED,
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
            wraplength=460,
            justify="left",
        )
        self.self_check_label.pack(fill="x", padx=14, pady=(2, 4))
        self._line(status_card, "今日归档", self.today_var)
        self._line(status_card, "热键", self.hotkey_var)
        self._line(status_card, "状态更新", self.updated_var)

        # 最近归档
        recent_card = self._card(self.root, "最近归档")
        recent_card.pack(fill="both", expand=True, padx=16, pady=6)
        self.recent_text = tk.Text(
            recent_card,
            height=5,
            bd=0,
            bg=COLOR_CARD,
            fg=COLOR_TEXT,
            font=("Microsoft YaHei UI", 9),
            wrap="word",
            state="disabled",
        )
        self.recent_text.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        # 错误
        error_card = self._card(self.root, "最近错误")
        error_card.pack(fill="x", padx=16, pady=6)
        tk.Label(
            error_card,
            textvariable=self.error_var,
            bg=COLOR_CARD,
            fg=COLOR_RED,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            justify="left",
            wraplength=460,
        ).pack(fill="x", padx=14, pady=(0, 10))

        # 控制按钮
        buttons = tk.Frame(self.root, bg=COLOR_BG)
        buttons.pack(side="bottom", fill="x", padx=16, pady=(4, 8))
        for text, command in (
            ("立即扫描", lambda: self._send("scan")),
            ("暂停/恢复", lambda: self._send("toggle_pause")),
            ("打开日志", self._open_log),
            ("知识库目录", self._open_folder),
        ):
            tk.Button(
                buttons,
                text=text,
                command=command,
                bg=COLOR_CARD,
                fg=COLOR_TEXT,
                activebackground="#eef2f7",
                activeforeground=COLOR_TEXT,
                relief="solid",
                bd=1,
                padx=10,
                pady=4,
                font=("Microsoft YaHei UI", 9),
            ).pack(side="left", padx=(0, 6))

        bottom = tk.Frame(self.root, bg=COLOR_BG)
        bottom.pack(side="bottom", fill="x", padx=16, pady=(0, 14))
        tk.Checkbutton(
            bottom,
            text="窗口置顶",
            variable=self.topmost_var,
            command=self._toggle_topmost,
            bg=COLOR_BG,
            fg=COLOR_TEXT,
            activebackground=COLOR_BG,
            font=("Microsoft YaHei UI", 9),
            selectcolor=COLOR_CARD,
        ).pack(side="left")
        tk.Button(
            bottom,
            text="刷新",
            command=self.refresh,
            bg=COLOR_CARD,
            fg=COLOR_TEXT,
            activebackground="#eef2f7",
            relief="solid",
            bd=1,
            padx=10,
            pady=3,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right")

    # ------------------------------------------------------------------ 行为
    def _toggle_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self.topmost_var.get()))

    def _send(self, command: str) -> None:
        try:
            status_bus.send_command(command)
        except Exception as exc:
            self.error_var.set(f"发送命令失败：{exc}")

    def _open_log(self) -> None:
        target = LOG_PATH if LOG_PATH.exists() else TRAY_LOG
        if not target.exists():
            target = OUTPUT_DIR
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception as exc:
            self.error_var.set(f"打开日志失败：{exc}")

    def _open_folder(self) -> None:
        try:
            os.startfile(str(OUTPUT_DIR))  # type: ignore[attr-defined]
        except Exception as exc:
            self.error_var.set(f"打开目录失败：{exc}")

    def refresh(self) -> None:
        data = status_bus.read_status()
        updated = data.get("updated_at", "")

        if not data:
            self.status_var.set("未连接")
            self.status_label.configure(fg=COLOR_ORANGE)
            self.game_var.set("🎮 全屏应用：未知")
            self.game_label.configure(fg=COLOR_MUTED)
            self.chat_var.set("托盘未运行")
            self.queue_var.set("-")
            self.countdown_var.set("-")
            self.self_check_var.set("微信 UI：未连接")
            self.self_check_label.configure(fg=COLOR_MUTED)
            self.updated_var.set("-")
            return

        paused = bool(data.get("paused"))
        scanning = bool(data.get("scanning"))
        if paused:
            self.status_var.set("已暂停")
            self.status_label.configure(fg=COLOR_ORANGE)
        elif scanning:
            self.status_var.set("正在扫描…")
            self.status_label.configure(fg="#2563eb")
        else:
            self.status_var.set("运行中")
            self.status_label.configure(fg=COLOR_GREEN)

        if data.get("game_mode"):
            self.game_var.set(
                f"🎮 当前处于全屏应用，自动化已挂起："
                f"{_fmt(data.get('game_reason'), '全屏应用')}"
            )
            self.game_label.configure(fg=COLOR_ORANGE)
        else:
            self.game_var.set("🎮 全屏应用：否")
            self.game_label.configure(fg=COLOR_GREEN)

        now = time.time()

        def remain(key: str) -> int:
            try:
                epoch = float(data.get(key) or 0)
            except (TypeError, ValueError):
                return 0
            return max(0, int(round(epoch - now))) if epoch > 0 else 0

        self.queue_var.set(f"{int(data.get('queue_depth') or 0)} 条")
        pause_reason = _fmt(data.get("auto_pause_reason"), "")
        if pause_reason:
            self.countdown_var.set(f"自动暂停：{pause_reason}")
        else:
            self.countdown_var.set(
                f"记录 {remain('next_scan_epoch')}s / "
                f"消息 {remain('next_message_epoch')}s"
            )
        due = remain("aggregate_due_epoch")
        if due > 0:
            self.countdown_var.set(
                f"{self.countdown_var.get()} / 聚合 {due}s"
            )

        if data.get("self_check_error"):
            self.self_check_var.set(f"⚠ {data['self_check_error']}")
            self.self_check_label.configure(fg=COLOR_RED)
        elif data.get("self_check_ok"):
            self.self_check_var.set("微信 UI：正常")
            self.self_check_label.configure(fg=COLOR_GREEN)
        else:
            self.self_check_var.set("微信 UI：自检中…")
            self.self_check_label.configure(fg=COLOR_MUTED)

        self.chat_var.set(_fmt(data.get("current_chat"), "未识别"))
        self.scan_var.set(_fmt(data.get("last_scan_at"), "尚未扫描"))
        self.message_var.set(_fmt(data.get("last_message_scan_at"), "尚未扫描"))
        self.today_var.set(str(data.get("today_archived", 0)))
        self.hotkey_var.set(_fmt(data.get("hotkeys"), "-"))
        self.updated_var.set(updated or "-")
        self.error_var.set(_fmt(data.get("last_error"), "无"))

        recent = data.get("recent_files") or []
        self.recent_text.configure(state="normal")
        self.recent_text.delete("1.0", "end")
        if recent:
            for item in recent[:8]:
                self.recent_text.insert("end", f"• {item}\n")
        else:
            self.recent_text.insert("end", "暂无归档记录\n")
        self.recent_text.configure(state="disabled")

    def _tick(self) -> None:
        self.refresh()
        self.root.after(1000, self._tick)


def self_test() -> int:
    root = tk.Tk()
    StatusPanel(root)
    root.after(500, root.destroy)
    root.mainloop()
    print("panel self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv and "--self-test" in argv:
        return self_test()

    root = tk.Tk()
    StatusPanel(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
