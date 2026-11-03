# -*- coding: utf-8 -*-
"""
ingest.py —— 微信知识库统一写入管线（通道无关）

所有采集通道（通知、热键剪贴板、记录窗口、PDF、OCR）最终都调用：
    pipeline.submit(IngestItem(...))

它负责：
    1. 归一化正文；
    2. 按天内容指纹去重（.processed.jsonl）；
    3. 同一来源 10 秒防抖合并，遇到日程/知识意图冲突立即切批；
    4. 后台线程调用 DeepSeek 分类，失败降级为 待分类-HHMMSS；
    5. 原子写 Markdown + 附件复制到 images/；
    6. 追加 manifest（通道、指纹、附件、是否降级），便于排查与断点续采。

设计约束（与项目红线一致）：
    - 只写 D:\\KnowledgeBase，不碰微信数据；
    - 不发消息、不读数据库、不 Hook；
    - 单线程写盘，避免并发覆盖。

CLI 自测：
    .\\venv\\Scripts\\python.exe ingest.py --source 锦 --text "明天下午3点开会"
    .\\venv\\Scripts\\python.exe ingest.py --source 锦 --file note.txt --attachment a.png
    .\\venv\\Scripts\\python.exe ingest.py --dry-run --text "..."
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(PROJECT_ROOT / ".env")

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

# #############################################################################
# 配置区
# #############################################################################
OUTPUT_DIR = Path(r"D:\KnowledgeBase")
IMAGES_DIR = OUTPUT_DIR / "images"
STAGING_DIR = OUTPUT_DIR / ".staging"
PROCESSED_FILE = OUTPUT_DIR / ".processed.jsonl"
MANIFEST_FILE = OUTPUT_DIR / ".manifest.jsonl"
LOG_PATH = OUTPUT_DIR / "ingest.log"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

AI_TIMEOUT = 6.0
AI_MAX_RETRY = 2
MAX_PROMPT_CHARS = 2000
DEBOUNCE_SECONDS = 10.0
FALLBACK_TYPE = "knowledge"
FALLBACK_TITLE_PREFIX = "待分类"
MAX_TITLE_LEN = 10

# 意图预判：只用于防抖批次的冲突打断，最终分类仍由 AI 决定
SCHEDULE_KEYWORDS = (
    "明天", "后天", "今天", "今晚", "上午", "下午", "晚上", "点", "开会", "会议",
    "日程", "预约", "提醒", "截止", "安排", "周", "月", "号",
)
KNOWLEDGE_KEYWORDS = (
    "知识", "教程", "原理", "笔记", "资料", "学习", "概念", "总结", "文章",
    "链接", "方法", "技巧", "经验", "摘录",
)

SYSTEM_PROMPT = """你是一个微信消息整理助手。用户会给你一段微信消息原文（可能是同一人连续发的多条消息合并而成），\
你需要判断它属于哪一类，并给出一个不超过 10 个字的标题。

分类规则（二选一）：
- schedule：包含时间、日期、地点、约定、提醒、待办、任务、截止时间等安排类信息；
- knowledge：观点、资料、教程、经验、概念、摘录、链接等值得长期归档的知识类信息。

输出要求（严格遵守）：
1. 只输出一个 JSON 对象，不要输出 Markdown 代码块、解释或任何多余文字；
2. 格式固定为 {"type": "schedule", "title": "标题"}；
3. type 只能是 "schedule" 或 "knowledge"；
4. title 必须是中文、准确概括核心内容、不超过 10 个字，且不能包含标点符号和空格。"""

LOG = logging.getLogger("ingest")

_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")
_INVISIBLE = re.compile(r"[\x00-\x1f\x7f\u200b-\u200d\ufeff]")
_CN_PUNCT = re.compile(r"[，。、；：！？“”‘’（）【】《》〈〉·…—～]")


# =============================================================================
# 基础设施
# =============================================================================
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
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
    except OSError:
        pass


def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sanitize_title(raw: str) -> str:
    title = _INVISIBLE.sub("", raw or "")
    title = _ILLEGAL_FILENAME.sub("", title)
    title = _CN_PUNCT.sub("", title)
    title = _WHITESPACE.sub("", title)
    return title.strip(".-")[:MAX_TITLE_LEN]


def content_fingerprint(source: str, text: str, when: datetime | None = None) -> str:
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{source}|{text}|{day}".encode("utf-8")).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def next_available_path(title: str, when: datetime) -> Path:
    stem = f"{when.strftime('%Y-%m-%d')}-{title or '未命名'}"
    path = OUTPUT_DIR / f"{stem}.md"
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = OUTPUT_DIR / f"{stem}-{index}.md"
        if not candidate.exists():
            return candidate
    raise RuntimeError("同名文件过多")


class FingerprintStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.processed: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        broken = 0
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    broken += 1
                    continue
                fp = record.get("fp") if isinstance(record, dict) else None
                if isinstance(fp, str) and fp:
                    self.processed.add(fp)
        if broken:
            LOG.warning("指纹文件有 %d 行损坏，已忽略", broken)

    def __contains__(self, fingerprint: str) -> bool:
        return fingerprint in self.processed

    def add(self, fingerprint: str, filename: str, source: str, when: datetime) -> None:
        with self._lock:
            if fingerprint in self.processed:
                return
            self.processed.add(fingerprint)
            record = {
                "fp": fingerprint,
                "file": filename,
                "source": source,
                "date": when.strftime("%Y-%m-%d %H:%M:%S"),
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                LOG.exception("写入指纹失败（Markdown 已落盘）")


def append_manifest(record: dict[str, Any]) -> None:
    try:
        MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MANIFEST_FILE, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        LOG.exception("写 manifest 失败")


# =============================================================================
# 意图预判
# =============================================================================
def quick_intent(text: str) -> str | None:
    if not text:
        return None
    schedule_hits = sum(1 for kw in SCHEDULE_KEYWORDS if kw in text)
    knowledge_hits = sum(1 for kw in KNOWLEDGE_KEYWORDS if kw in text)
    if schedule_hits == knowledge_hits:
        return None
    return "schedule" if schedule_hits > knowledge_hits else "knowledge"


def intent_conflict(old: str | None, new: str | None) -> bool:
    return bool(old and new and old != new)


# =============================================================================
# AI 分类
# =============================================================================
_CLIENT: Any = None
_CLIENT_LOCK = threading.Lock()


def get_client():
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = OpenAI(
                    api_key=DEEPSEEK_API_KEY,
                    base_url=DEEPSEEK_BASE_URL,
                    timeout=AI_TIMEOUT,
                    max_retries=0,
                )
    return _CLIENT


def parse_ai_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise ValueError(f"找不到 JSON：{raw[:80]!r}")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("返回不是对象")
    return data


def normalize_type(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"schedule", "日程", "calendar", "event", "task", "todo"}:
        return "schedule"
    if raw in {"knowledge", "知识", "note", "notes"}:
        return "knowledge"
    return None


def classify_message(text: str) -> tuple[str, str] | None:
    if not DEEPSEEK_API_KEY or OpenAI is None:
        LOG.warning("未配置 DEEPSEEK_API_KEY 或缺少 openai，降级归档")
        return None

    prompt_text = text[:MAX_PROMPT_CHARS]
    last_error: Exception | None = None

    for attempt in range(1, AI_MAX_RETRY + 1):
        try:
            response = get_client().chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"消息原文：\n{prompt_text}"},
                ],
                temperature=0.0,
                max_tokens=80,
                response_format={"type": "json_object"},
            )
            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                raise ValueError("模型返回为空")
            data = parse_ai_json(raw)
            doc_type = normalize_type(data.get("type"))
            if doc_type is None:
                raise ValueError(f"非法 type：{data.get('type')!r}")
            title = sanitize_title(str(data.get("title") or ""))
            if not title:
                raise ValueError("标题为空")
            return doc_type, title
        except Exception as exc:
            last_error = exc
            LOG.warning("AI 分类第 %d/%d 次失败：%s", attempt, AI_MAX_RETRY, exc)
            if attempt < AI_MAX_RETRY:
                time.sleep(1.0)

    LOG.error("AI 分类失败（%s），降级为 %s", last_error, FALLBACK_TYPE)
    return None


# =============================================================================
# 数据结构
# =============================================================================
@dataclass
class IngestItem:
    channel: str                 # notify / hotkey / record / pdf / ocr / manual
    source: str                  # 会话名（如「锦」）
    text: str
    sender: str = ""
    when: datetime = field(default_factory=datetime.now)
    attachments: list[str] = field(default_factory=list)
    dedup_key: str = ""          # 通道提供的稳定 id；为空则用内容哈希
    immediate: bool = False      # 记录窗口/PDF 这类完整文档不再防抖
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Batch:
    source: str
    sender: str
    channel: str
    parts: list[tuple[str, str]] = field(default_factory=list)  # (text, fingerprint)
    attachments: list[str] = field(default_factory=list)
    when: datetime = field(default_factory=datetime.now)
    intent: str | None = None
    timer: threading.Timer | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# 管线
# =============================================================================
class Pipeline:
    def __init__(self, dry_run: bool = False) -> None:
        setup_logging()  # 作为库被调用时也要有日志输出
        self.dry_run = dry_run
        self.store = FingerprintStore(PROCESSED_FILE)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")
        self._lock = threading.Lock()
        self._buffers: dict[str, _Batch] = {}
        self._inflight: set[str] = set()
        self._futures: set[Future] = set()
        self._closed = False
        self.accepted = 0

    # ------------------------------------------------------------------ 提交
    def submit(self, item: IngestItem) -> bool:
        text = normalize_text(item.text)
        if not text:
            return False

        fingerprint = item.dedup_key or content_fingerprint(
            item.source, text, item.when
        )

        to_dispatch: _Batch | None = None
        conflict_batch: _Batch | None = None

        with self._lock:
            if fingerprint in self.store or fingerprint in self._inflight:
                LOG.info("跳过重复：%s", text[:30])
                return False

            if self._closed:
                return False

            self._inflight.add(fingerprint)

            if item.immediate:
                to_dispatch = _Batch(
                    source=item.source,
                    sender=item.sender,
                    channel=item.channel,
                    parts=[(text, fingerprint)],
                    attachments=list(item.attachments),
                    when=item.when,
                    intent=quick_intent(text),
                    meta=dict(item.meta),
                )
            else:
                batch = self._buffers.get(item.source)
                new_intent = quick_intent(text)

                if batch is not None and intent_conflict(batch.intent, new_intent):
                    # 意图冲突：旧批次立刻落盘，新消息开新批次
                    if batch.timer is not None:
                        batch.timer.cancel()
                    self._buffers.pop(item.source, None)
                    conflict_batch = batch
                    batch = None

                if batch is None:
                    batch = _Batch(
                        source=item.source,
                        sender=item.sender,
                        channel=item.channel,
                        when=item.when,
                        intent=new_intent,
                        meta=dict(item.meta),
                    )
                    self._buffers[item.source] = batch
                elif batch.intent is None:
                    batch.intent = new_intent

                batch.parts.append((text, fingerprint))
                batch.attachments.extend(item.attachments)

                if batch.timer is not None:
                    batch.timer.cancel()
                timer = threading.Timer(
                    DEBOUNCE_SECONDS, self._flush, args=(item.source,)
                )
                timer.daemon = True
                batch.timer = timer
                timer.start()

        if conflict_batch is not None:
            LOG.info(
                "意图冲突（%s -> %s），旧批次立即落盘",
                conflict_batch.intent,
                quick_intent(text),
            )
            self._dispatch(conflict_batch)
        if to_dispatch is not None:
            self._dispatch(to_dispatch)

        return True

    def _flush(self, source: str) -> None:
        with self._lock:
            batch = self._buffers.pop(source, None)
            if batch is None:
                return
            if batch.timer is not None:
                batch.timer.cancel()

        self._dispatch(batch)

    def _dispatch(self, batch: _Batch) -> None:
        text = "\n".join(part for part, _fp in batch.parts).strip()
        if not text:
            return

        self.accepted += 1
        merged = _Batch(
            source=batch.source,
            sender=batch.sender,
            channel=batch.channel,
            parts=list(batch.parts),
            attachments=list(batch.attachments),
            when=batch.when,
            intent=batch.intent,
            meta=dict(batch.meta),
        )
        future = self.executor.submit(self._process, merged, text)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._on_done)

    def _on_done(self, future: Future) -> None:
        with self._lock:
            self._futures.discard(future)
        try:
            future.result()
        except Exception:
            LOG.exception("后台落盘失败")

    # ------------------------------------------------------------------ 落盘
    def _process(self, batch: _Batch, text: str) -> None:
        try:
            self._process_inner(batch, text)
        finally:
            for _t, fp in batch.parts:
                self._inflight.discard(fp)

    def _process_inner(self, batch: _Batch, text: str) -> None:
        result = classify_message(text)
        if result is None:
            doc_type = FALLBACK_TYPE
            title = f"{FALLBACK_TITLE_PREFIX}-{batch.when.strftime('%H%M%S')}"
        else:
            doc_type, title = result

        staged, run_dir = self._stage_attachments(batch.attachments, batch.when)
        attachments = [rel for _src, rel in staged]

        body = text
        if batch.sender:
            body = f"**{batch.sender}**\n\n{text}"

        if self.dry_run:
            LOG.info(
                "[DRY-RUN] [%s] %s <- %s（%d 字，%d 图，通道 %s）",
                doc_type,
                title,
                batch.source,
                len(text),
                len(attachments),
                batch.channel,
            )
            self._cleanup_staging(run_dir)
            return

        try:
            path = self._write_markdown(doc_type, title, batch, body, attachments)
        except Exception:
            # Markdown 没写成，临时图片全部清掉，避免孤儿
            self._cleanup_staging(run_dir)
            raise

        # Markdown 已经原子落盘，再把图片移入 images/
        self._finalize_attachments(staged, run_dir)

        for _t, fp in batch.parts:
            self.store.add(fp, path.name, batch.source, batch.when)

        append_manifest(
            {
                "file": path.name,
                "channel": batch.channel,
                "source": batch.source,
                "sender": batch.sender,
                "type": doc_type,
                "title": title,
                "chars": len(text),
                "messages": len(batch.parts),
                "attachments": attachments,
                "fallback": result is None,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                **batch.meta,
            }
        )
        LOG.info("已归档 [%s] %s（%d 条合并）", doc_type, path, len(batch.parts))

    def _stage_attachments(
        self, attachments: list[str], when: datetime
    ) -> tuple[list[tuple[Path, str]], Path | None]:
        """
        把附件复制到 .staging，返回 [(临时路径, Markdown 相对路径)]。
        Markdown 写成功后才 os.replace 到 images/；失败则整个临时目录删除。
        """
        staged: list[tuple[Path, str]] = []
        if not attachments:
            return staged, None

        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        run_dir = STAGING_DIR / uuid.uuid4().hex
        run_dir.mkdir(parents=True, exist_ok=True)

        for raw in attachments:
            src = Path(raw)
            if not src.is_file():
                LOG.warning("附件不存在，跳过：%s", src)
                continue
            try:
                if src.parent.resolve() == IMAGES_DIR.resolve():
                    # 已经是最终文件，不需要再搬
                    staged.append((src, f"./images/{src.name}"))
                    continue
                name = (
                    f"{when.strftime('%Y%m%d-%H%M%S')}-"
                    f"{uuid.uuid4().hex[:6]}-{src.name}"
                )
                dst = run_dir / name
                shutil.copy2(src, dst)
                staged.append((dst, f"./images/{name}"))
            except OSError:
                LOG.exception("暂存附件失败：%s", src)
        return staged, run_dir

    def _cleanup_staging(self, run_dir: Path | None) -> None:
        if run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)

    def _finalize_attachments(
        self, staged: list[tuple[Path, str]], run_dir: Path | None
    ) -> None:
        for src, rel in staged:
            final = IMAGES_DIR / Path(rel).name
            if src.resolve() == final.resolve():
                continue
            try:
                os.replace(src, final)
            except OSError:
                LOG.exception("移动附件失败：%s -> %s", src, final)
        self._cleanup_staging(run_dir)

    def _write_markdown(
        self,
        doc_type: str,
        title: str,
        batch: _Batch,
        body: str,
        attachments: list[str],
    ) -> Path:
        tags = ["微信采集", doc_type]
        frontmatter = [
            "---",
            f"type: {doc_type}",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"source: {json.dumps(batch.source, ensure_ascii=False)}",
            f"date: {batch.when.strftime('%Y-%m-%d %H:%M:%S')}",
            f"tags: {json.dumps(tags, ensure_ascii=False)}",
            f"channel: {batch.channel}",
            "---",
        ]
        parts = ["\n".join(frontmatter), body.rstrip()]
        for rel in attachments:
            parts.append(f"![图片]({rel})")
        markdown = "\n\n".join(parts) + "\n"

        for _ in range(10):
            path = next_available_path(title, batch.when)
            if not path.exists():
                atomic_write_text(path, markdown)
                return path
            time.sleep(0.2)
        raise RuntimeError("无法生成不重名文件")

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        with self._lock:
            self._closed = True
            sources = list(self._buffers)

        for source in sources:
            self._flush(source)

        self.executor.shutdown(wait=True)


# =============================================================================
# CLI
# =============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="微信知识库统一写入管线")
    parser.add_argument("--source", default="锦", help="会话名")
    parser.add_argument("--sender", default="", help="发送者（可选）")
    parser.add_argument("--channel", default="manual", help="通道标识")
    parser.add_argument("--text", default="", help="直接传入正文")
    parser.add_argument("--file", default="", help="从文本文件读取正文")
    parser.add_argument("--attachment", action="append", default=[], help="附件路径，可多次")
    parser.add_argument("--immediate", action="store_true", help="跳过防抖，立即落盘")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()

    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8-sig")
    if not text:
        text = sys.stdin.read()
    if not text.strip():
        print("没有正文")
        return 1

    pipeline = Pipeline(dry_run=args.dry_run)
    try:
        ok = pipeline.submit(
            IngestItem(
                channel=args.channel,
                source=args.source,
                sender=args.sender,
                text=text,
                attachments=args.attachment,
                immediate=True,  # CLI 单次调用直接落盘，不等待防抖
            )
        )
        print("submitted" if ok else "duplicate/empty")
    finally:
        pipeline.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
