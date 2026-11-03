# -*- coding: utf-8 -*-
"""
message_watcher.py —— 「锦」会话里的普通单条消息聚合抓取

只做保守采集：
    - 只扫描当前「锦」会话右侧聊天区、标题栏以下、输入框以上；
    - 首次运行只“学习”当前可见消息，不归档历史；
    - 只把新出现的 OCR 行放入缓冲；
    - 15 秒内的连续消息合并成一段；
    - 纯文本长度 <= 10 且不含链接 -> 丢弃；
    - 图片只取当前可见区域最下面的一张，每个聚合窗口最多一张；
    - 每小时最多提交 40 次，避免 AI 接口爆炸。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import record_capture as rc
import status_bus
from chat_watcher import EXPECTED_CHAT, current_chat_name, find_main_window
from ingest import IngestItem, Pipeline

OUTPUT_DIR = Path(r"D:\KnowledgeBase")
IMAGES_DIR = OUTPUT_DIR / "images"
SEEN_FILE = OUTPUT_DIR / ".chat_seen.json"

MIN_TEXT_LEN = 10
AGGREGATE_SECONDS = 15.0
MAX_TEXT_LEN = 4000
MAX_IMAGES_PER_WINDOW = 1
MAX_SUBMISSIONS_PER_HOUR = 40

LINK_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
NOISE_RE = re.compile(
    r"^(收到|好的|好|嗯|哦|噢|哈哈+|嘿嘿+|在吗|谢谢|感谢|ok|OK|"
    r"\[图片\]|\[表情\]|\[语音\]|\[文件\]|\[聊天记录\])$"
)
TIME_RE = re.compile(r"\d{1,4}\s*年|\d{1,2}\s*月|\d{1,2}\s*[:：]\s*\d{2}")
SYSTEM_RE = re.compile(r"以下为新消息|查看更多消息|撤回了一条消息|对方正在输入|聊天信息")

LOG_PREFIX = "[message_watcher]"


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(SEEN_FILE)


def is_meaningful(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    if LINK_RE.search(value):
        return True
    if len(value) <= MIN_TEXT_LEN:
        return False
    if NOISE_RE.fullmatch(value):
        return False
    return True


def _is_outgoing(image, box: tuple[float, float, float, float]) -> bool:
    """绿色气泡判定：小号自己发的消息是绿底黑字，大号发来的是白底。"""
    top, left, right, bottom = box
    crop = image.crop((int(left), int(top), int(right), int(bottom))).convert("RGB")
    pixels = list(crop.getdata())
    if not pixels:
        return False
    r = sum(p[0] for p in pixels) / len(pixels)
    g = sum(p[1] for p in pixels) / len(pixels)
    b = sum(p[2] for p in pixels) / len(pixels)
    return g > r * 1.18 and g > b * 1.25


def _candidate_lines(image) -> list[str]:
    """
    只取聊天区左侧（对方）的正文 OCR 行，排除：
    标题/输入框/时间/系统提示/右侧绿色气泡（自己发的）。
    """
    boxes = asyncio.run(rc.ocr_boxes(image))
    top_limit = image.height * 0.12
    bottom_limit = image.height * 0.78
    left_limit = image.width * 0.35
    incoming_limit = image.width * 0.62

    lines: list[str] = []
    for top, left, right, bottom, text in boxes:
        if top < top_limit or top > bottom_limit:
            continue
        if left < left_limit or left > incoming_limit:
            continue
        if _is_outgoing(image, (top, left, right, bottom)):
            continue
        value = rc.clean_ocr_line(text)
        if len(value) < 2:
            continue
        if TIME_RE.search(value):
            continue
        if SYSTEM_RE.search(value):
            continue
        if NOISE_RE.fullmatch(value):
            continue
        lines.append(value)
    return lines


def _last_image_crop(image):
    """取当前聊天区最下面的一张图，返回 (裁剪图, 内容签名)。"""
    boxes = rc.detect_image_regions(image)
    candidates = [
        box
        for box in boxes
        if image.height * 0.12 < box[1] < image.height * 0.78
    ]
    if not candidates:
        return None

    left, top, right, bottom = max(candidates, key=lambda b: b[1])
    crop = image.crop((left, top, right, bottom))
    signature = hashlib.sha256(crop.tobytes()).hexdigest()[:20]
    return crop, signature


class MessageAggregator:
    def __init__(self, pipeline: Pipeline, dry_run: bool = False) -> None:
        self.pipeline = pipeline
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._image: str | None = None
        self._timer: threading.Timer | None = None
        self._submissions: list[float] = []
        self._closed = False

    def add(self, text: str, image: str | None = None) -> None:
        value = text.strip()
        if not value and not image:
            return

        with self._lock:
            if self._closed:
                return
            if value:
                self._parts.append(value)
            if image and self._image is None:
                self._image = image

            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(AGGREGATE_SECONDS, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _allowed(self) -> bool:
        now = time.time()
        self._submissions = [t for t in self._submissions if now - t < 3600]
        return len(self._submissions) < MAX_SUBMISSIONS_PER_HOUR

    def _flush(self) -> None:
        with self._lock:
            parts = list(self._parts)
            image = self._image
            self._parts.clear()
            self._image = None
            self._timer = None

        text = "\n".join(parts).strip()
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN] + "\n…（已截断）"

        if not is_meaningful(text) and not image:
            log(f"丢弃低价值聚合：{text[:40]!r}")
            return
        if not self._allowed():
            log("本小时提交次数已达上限，丢弃本次聚合")
            return

        payload = text if text else "[图片]"
        log(
            f"提交聚合：{len(payload)} 字，图片 {1 if image else 0} 张，"
            f"预览={payload[:50]!r}"
        )
        if self.dry_run:
            return

        ok = self.pipeline.submit(
            IngestItem(
                channel="chat",
                source=EXPECTED_CHAT,
                text=payload,
                attachments=[image] if image else [],
                immediate=True,
            )
        )
        if ok:
            self._submissions.append(time.time())
            status_bus.write_status(
                last_message_scan_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                last_message_result="已提交聚合",
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
            has_content = bool(self._parts or self._image)
        if has_content:
            self._flush()


class MessageWatcher:
    def __init__(self, pipeline: Pipeline, dry_run: bool = False) -> None:
        self.pipeline = pipeline
        self.dry_run = dry_run
        self.aggregator = MessageAggregator(pipeline, dry_run=dry_run)
        self.seen = load_seen()
        self._learned = bool(self.seen)
        self.last_image_sig: str | None = None

    def learn(self) -> None:
        main = find_main_window()
        if not main:
            log("没有微信主窗口")
            return
        hwnd, _title = main
        image = rc.capture_window(hwnd)
        if current_chat_name(image) != EXPECTED_CHAT:
            log("当前不是「锦」，跳过学习")
            return
        for line in _candidate_lines(image):
            self.seen.add(_hash(line))
        image_crop = _last_image_crop(image)
        if image_crop is not None:
            self.last_image_sig = image_crop[1]
        save_seen(self.seen)
        self._learned = True
        log(f"已学习当前可见消息 {len(self.seen)} 条")

    def scan_once(self) -> int:
        main = find_main_window()
        if not main:
            log("没有微信主窗口")
            return 1
        hwnd, _title = main

        image = rc.capture_window(hwnd)
        chat_name = current_chat_name(image)
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if chat_name != EXPECTED_CHAT:
            log(f"当前聊天是 {chat_name!r}，跳过普通消息采集")
            status_bus.write_status(
                current_chat=chat_name or "未识别",
                last_message_scan_at=now_text,
                last_message_result="非锦会话，跳过",
            )
            return 0

        lines = _candidate_lines(image)

        # 第一次运行只学习，避免把历史消息全量归档
        if not self._learned:
            for line in lines:
                self.seen.add(_hash(line))
            image_crop = _last_image_crop(image)
            if image_crop is not None:
                self.last_image_sig = image_crop[1]
            save_seen(self.seen)
            self._learned = True
            log(f"首次运行：学习当前可见消息 {len(self.seen)} 条，不归档")
            status_bus.write_status(
                current_chat=chat_name,
                last_message_scan_at=now_text,
                last_message_result="已学习当前消息",
            )
            return 0

        new_lines: list[str] = []
        for line in lines:
            digest = _hash(line)
            if digest in self.seen:
                continue
            self.seen.add(digest)
            new_lines.append(line)

        image_path: str | None = None
        image_crop = _last_image_crop(image)
        if image_crop is not None:
            crop, signature = image_crop
            if self.last_image_sig is None:
                # 进程刚启动：把当前图当基线，避免把旧图挂到新消息上
                self.last_image_sig = signature
            elif signature != self.last_image_sig:
                IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                name = (
                    f"chat-image-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
                )
                path = IMAGES_DIR / name
                crop.save(path, format="PNG")
                self.last_image_sig = signature
                image_path = str(path)

        if not new_lines and image_path is None:
            status_bus.write_status(
                current_chat=chat_name,
                last_message_scan_at=now_text,
                last_message_result="无新增",
            )
            return 0

        for line in new_lines:
            self.aggregator.add(line)
        if image_path:
            self.aggregator.add("", image_path)
        save_seen(self.seen)

        log(
            f"新增 {len(new_lines)} 行，图片 {1 if image_path else 0} 张，"
            f"等待 {int(AGGREGATE_SECONDS)} 秒聚合"
        )
        status_bus.write_status(
            current_chat=chat_name,
            last_message_scan_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_message_result=f"新增 {len(new_lines)} 行",
        )
        return 0

    def close(self) -> None:
        self.aggregator.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="普通单条消息过滤聚合")
    parser.add_argument("--once", action="store_true", help="立即扫描一次")
    parser.add_argument("--learn", action="store_true", help="把当前可见消息标记为已读")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不提交")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pipeline = Pipeline(dry_run=args.dry_run)
    watcher = MessageWatcher(pipeline, dry_run=args.dry_run)
    try:
        if args.learn:
            watcher.learn()
            return 0
        if args.once:
            return watcher.scan_once()
        watcher.scan_once()
        return 0
    finally:
        watcher.close()
        pipeline.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
