# -*- coding: utf-8 -*-
"""
wechat_collector.py —— 微信消息采集器 V2（微信 4.x / wechatauto-replica）

V2.0 新增：
    1. 防抖合并：同一联系人 10 秒内的连续消息合并成一条，只调一次 AI、只写一个文件；
    2. 图片归档：type == 'image' 时通过 MediaDownloader 落盘到
       D:\\KnowledgeBase\\images\\，正文写入 [图片: ./images/xxx.jpg]；
    3. 合并转发：识别聊天记录卡片（appmsg type=19 / recorditem），
       尽量提取里面的文本；提取不到则保留占位 [合并转发的聊天记录]，绝不丢弃。

保留的安全机制：
    - 启动 + 每 30 秒的账号校验（切号立即退出，退出码 3）；
    - 监听线程健康检查、异常补处理、重建监听；
    - JSONL 按天指纹去重、AI 降级兜底、Markdown 原子写、文件名防覆盖；
    - 防封随机 sleep：已从「每收一条」移到「合并后、AI 请求前」，每批只等一次。

--------------------------------------------------------------------------
安装 / 迁移
--------------------------------------------------------------------------
    .\\venv\\Scripts\\pip.exe uninstall -y wxauto        # 若曾 pip 安装过
    Rename-Item .\\wxauto .\\wxauto_bak                  # 本项目是源码目录时
    .\\venv\\Scripts\\pip.exe install -i https://pypi.tuna.tsinghua.edu.cn/simple wechatauto-replica==1.2.9

注意：发行包叫 wechatauto-replica，导入名是 wechatauto。

.env（与脚本同目录）：
    DEEPSEEK_API_KEY=sk-你的密钥
    DEEPSEEK_BASE_URL=https://api.deepseek.com
    DEEPSEEK_MODEL=deepseek-chat

.gitignore：
    venv/
    __pycache__/
    *.pyc
    .env
    *.log
    .processed.jsonl
    .processed.json
    images/

requirements.txt：
    wechatauto-replica==1.2.9
    fastapi==0.162.0
    uvicorn==0.54.0
    python-frontmatter==1.1.0
    openai==3.42.1
    pywin32==312
    uiautomation==2.0.29
    comtypes==1.4.17
    pillow==13.0.0
    pyperclip==1.11.0

运行：
    .\\venv\\Scripts\\python.exe wechat_collector.py
    .\\venv\\Scripts\\python.exe wechat_collector.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import html
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
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，真实环境变量优先。"""
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        logging.getLogger("wechat_collector").warning("读取 .env 失败：%s", exc)
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
    from wechatauto import WeChat  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "无法导入 wechatauto（微信 4.x 自动化库）。\n"
        f"原始错误：{exc}\n"
        "请先执行：\n"
        r"    .\venv\Scripts\pip.exe install -i "
        "https://pypi.tuna.tsinghua.edu.cn/simple wechatauto-replica==1.2.9"
    )

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    raise SystemExit("缺少依赖 openai，请执行：.\\venv\\Scripts\\pip.exe install openai")


# #############################################################################
# #                                                                           #
# #                     【 配 置 区 】 只改这里就够了                          #
# #                                                                           #
# #############################################################################

# -----------------------------------------------------------------------------
# 1) 监听白名单（会话显示名）
# -----------------------------------------------------------------------------
TARGET_FRIENDS = ['文件传输助手']

# -----------------------------------------------------------------------------
# 2) DeepSeek 配置
# -----------------------------------------------------------------------------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

# -----------------------------------------------------------------------------
# 3) 是否采集自己发出的消息（attr == 'self'）
# -----------------------------------------------------------------------------
INCLUDE_SELF_MESSAGE = True

# -----------------------------------------------------------------------------
# 4) 账号身份校验
# -----------------------------------------------------------------------------
EXPECTED_WECHAT_ID = ""
EXPECTED_WECHAT_NICKNAME = "锦的小号"
IDENTITY_RECHECK_SECONDS = 30.0
LISTENER_HEALTH_CHECK_SECONDS = 60.0

# -----------------------------------------------------------------------------
# 5) 防抖合并：同一联系人静默多少秒后合并落盘
# -----------------------------------------------------------------------------
DEBOUNCE_SECONDS = 10.0

# -----------------------------------------------------------------------------
# 6) 存储与运行参数
# -----------------------------------------------------------------------------
OUTPUT_DIR = Path(r"D:\KnowledgeBase")
IMAGES_DIR = OUTPUT_DIR / "images"          # 图片落盘目录
LOG_PATH = OUTPUT_DIR / "collector.log"
PROCESSED_FILE = OUTPUT_DIR / ".processed.jsonl"
LEGACY_PROCESSED_FILE = OUTPUT_DIR / ".processed.json"

TEXT_MESSAGE_TYPES = {"text", "quote"}

MAX_TITLE_LEN = 10
MAX_PROMPT_CHARS = 2000
AI_TIMEOUT = 6.0
AI_MAX_RETRY = 2
MESSAGE_SLEEP = (2.0, 5.0)          # 防封：合并后、AI 请求前随机等待
KEEPALIVE_SLEEP = 1.0
ERROR_SLEEP = 5.0
RECOVERY_WINDOW_MINUTES = 30
FALLBACK_TYPE = "knowledge"
FALLBACK_TITLE_PREFIX = "待分类"

# 运行模式（main 里根据 --dry-run 设置）：dry-run 时图片也不落盘
DRY_RUN = False

# #############################################################################
# #                            配置区结束，以下无需修改                        #
# #############################################################################

LOG = logging.getLogger("wechat_collector")

_PLACEHOLDER_PATTERN = re.compile(r"^\[[^\[\]]{1,12}\]$")
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")
_INVISIBLE_CHARS = re.compile(r"[\x00-\x1f\x7f\u200b-\u200d\ufeff]")
_CN_PUNCTUATION = re.compile(r"[，。、；：！？“”‘’（）【】《》〈〉·…—～]")

SYSTEM_PROMPT = """你是一个微信消息整理助手。用户会给你一段微信消息原文（可能由同一人连续发的多条消息合并而成），\
你需要判断它属于哪一类，并给出一个不超过 10 个字的标题。

分类规则（二选一）：
- schedule：包含时间、日期、地点、约定、提醒、待办、任务、截止时间等安排类信息；
- knowledge：观点、资料、教程、经验、概念、摘录、链接等值得长期归档的知识类信息。

输出要求（严格遵守）：
1. 只输出一个 JSON 对象，不要输出 Markdown 代码块、解释或任何多余文字；
2. 格式固定为 {"type": "schedule", "title": "标题"}；
3. type 只能是 "schedule" 或 "knowledge"；
4. title 必须是中文、准确概括核心内容、不超过 10 个字，且不能包含标点符号和空格。"""


# =============================================================================
# 日志
# =============================================================================
def setup_logging() -> None:
    if LOG.handlers:
        return

    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    LOG.addHandler(console)

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        LOG.addHandler(file_handler)
    except OSError as exc:
        LOG.warning("日志文件不可用（%s），仅输出到控制台", exc)


def prepare_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# =============================================================================
# 账号身份确认
# =============================================================================
class AccountSwitchedError(RuntimeError):
    """运行期间检测到微信账号被切换，立即终止。"""


@dataclass(frozen=True)
class WeChatIdentity:
    nickname: str
    wxid: str


def read_identity(wx: WeChat) -> WeChatIdentity:
    """优先读公开的 nickname；wxid 读私有 _wxid，失败再用 DB 兜底。"""
    nickname = str(getattr(wx, "nickname", "") or "").strip()

    # TODO: 请查阅 wechatauto-replica 文档确认是否有公开的 wxid 接口
    wxid = str(getattr(wx, "_wxid", "") or "").strip()

    if not wxid or not nickname:
        db = getattr(wx, "_db", None)
        if db is not None:
            try:
                info = db.get_self_info() or {}
                wxid = wxid or str(info.get("username") or "").strip()
                nickname = nickname or str(info.get("nick_name") or "").strip()
            except Exception:
                LOG.warning("读取账号信息失败（get_self_info）", exc_info=True)

    return WeChatIdentity(nickname=nickname, wxid=wxid)


def verify_identity(identity: WeChatIdentity) -> bool:
    if EXPECTED_WECHAT_ID:
        if not identity.wxid:
            LOG.error("已配置 EXPECTED_WECHAT_ID，但读不到微信号，脚本退出")
            return False
        if identity.wxid != EXPECTED_WECHAT_ID:
            LOG.error(
                "账号校验失败：期望微信号 %s，当前是 %s（昵称 %s），脚本退出",
                EXPECTED_WECHAT_ID,
                identity.wxid,
                identity.nickname or "<未知>",
            )
            return False
        LOG.info("微信号校验通过：%s", identity.wxid)

    if EXPECTED_WECHAT_NICKNAME:
        if identity.nickname != EXPECTED_WECHAT_NICKNAME:
            LOG.error(
                "账号校验失败：期望昵称 %s，当前是 %s，脚本退出",
                EXPECTED_WECHAT_NICKNAME,
                identity.nickname or "<未知>",
            )
            return False
        LOG.info("昵称校验通过：%s", identity.nickname)

    return True


def print_identity_banner(identity: WeChatIdentity) -> None:
    box = [
        "#" * 72,
        f"#  当前绑定的微信账号：{identity.nickname or '<未知>'}",
        f"#  微信号（wxid）：{identity.wxid or '<未读取到>'}",
        f"#  监听对象：{'、'.join(TARGET_FRIENDS) or '<空>'}",
        "#" * 72,
    ]
    print("\n" + "\n".join(box) + "\n", flush=True)
    LOG.info(
        "当前绑定的微信账号：%s（wxid=%s）",
        identity.nickname or "<未知>",
        identity.wxid or "<未知>",
    )


def print_startup_notice() -> None:
    box = [
        "!" * 72,
        "【提示】wechatauto-replica 读取本地数据库，不操控前台窗口；",
        "        但请确认 PC 微信登录的是采集小号，账号不符会立即退出。",
        "!" * 72,
    ]
    print("\n" + "\n".join(box) + "\n", flush=True)
    LOG.warning("启动提醒：请确认 PC 微信当前登录的是采集小号")


def check_account_unchanged(wx: WeChat, expected: WeChatIdentity) -> None:
    latest = read_identity(wx)

    if expected.nickname and latest.nickname and latest.nickname != expected.nickname:
        raise AccountSwitchedError(
            f"检测到微信账号已切换：启动时是 {expected.nickname}，"
            f"当前是 {latest.nickname}。为避免误操作大号，采集器立即停止。"
        )
    if expected.wxid and latest.wxid and latest.wxid != expected.wxid:
        raise AccountSwitchedError(
            f"检测到微信账号已切换：启动时 wxid={expected.wxid}，"
            f"当前 wxid={latest.wxid}。为避免误操作大号，采集器立即停止。"
        )


# =============================================================================
# 文件写入与标题清洗
# =============================================================================
def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def sanitize_title(raw: str) -> str:
    title = _INVISIBLE_CHARS.sub("", raw or "")
    title = _ILLEGAL_FILENAME_CHARS.sub("", title)
    title = _CN_PUNCTUATION.sub("", title)
    title = _WHITESPACE.sub("", title)
    title = title.strip(".-")
    return title[:MAX_TITLE_LEN]


def fallback_title(when: datetime) -> str:
    return f"{FALLBACK_TITLE_PREFIX}-{when.strftime('%H%M%S')}"


def next_available_path(title: str, when: datetime) -> Path:
    stem = f"{when.strftime('%Y-%m-%d')}-{title or '未命名'}"
    path = OUTPUT_DIR / f"{stem}.md"
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = OUTPUT_DIR / f"{stem}-{index}.md"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"同名文件过多，无法生成新文件名：{stem}")


# =============================================================================
# DeepSeek 分类
# =============================================================================
_CLIENT: OpenAI | None = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> OpenAI:
    global _CLIENT
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
            raise ValueError(f"返回内容中找不到 JSON：{raw[:80]!r}")
        data = json.loads(match.group(0))

    if not isinstance(data, dict):
        raise ValueError("返回的 JSON 不是对象")
    return data


def normalize_type(value: Any) -> str | None:
    raw_type = str(value or "").strip().lower()
    if raw_type in {"schedule", "日程", "calendar", "event", "task", "todo"}:
        return "schedule"
    if raw_type in {"knowledge", "知识", "note", "notes"}:
        return "knowledge"
    return None


def classify_message(text: str) -> tuple[str, str] | None:
    if not DEEPSEEK_API_KEY:
        LOG.warning("未配置 DEEPSEEK_API_KEY，本条降级归档")
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
                raise ValueError("模型返回内容为空")

            data = parse_ai_json(raw)
            doc_type = normalize_type(data.get("type"))
            if doc_type is None:
                raise ValueError(f"非法 type：{data.get('type')!r}")

            title = sanitize_title(str(data.get("title") or ""))
            if not title:
                raise ValueError("模型返回的 title 为空或清洗后为空")

            return doc_type, title

        except Exception as exc:
            last_error = exc
            LOG.warning("DeepSeek 分类第 %d/%d 次失败：%s", attempt, AI_MAX_RETRY, exc)
            if attempt < AI_MAX_RETRY:
                time.sleep(1.0)

    LOG.error("DeepSeek 分类最终失败（%s），本条降级为 %s", last_error, FALLBACK_TYPE)
    return None


# =============================================================================
# 指纹库（JSONL）
# =============================================================================
def message_fingerprint(source: str, content: str, when: datetime | None = None) -> str:
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{source}|{content}|{day}".encode("utf-8")).hexdigest()


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
        try:
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
                    fingerprint = record.get("fp") if isinstance(record, dict) else None
                    if isinstance(fingerprint, str) and fingerprint:
                        self.processed.add(fingerprint)
        except OSError as exc:
            LOG.error("读取指纹文件失败（%s），本次按空库处理", exc)

        if broken:
            LOG.warning("指纹文件中有 %d 行损坏，已忽略", broken)

    def __contains__(self, fingerprint: str) -> bool:
        return fingerprint in self.processed

    def __len__(self) -> int:
        return len(self.processed)

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
                LOG.exception("追加指纹失败（Markdown 已落盘，重启后可能重复归档一次）")


# =============================================================================
# Markdown
# =============================================================================
def build_markdown(
    doc_type: str, title: str, source: str, content: str, when: datetime
) -> str:
    tags = ["微信采集", doc_type]
    frontmatter = [
        "---",
        f"type: {doc_type}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"source: {json.dumps(source, ensure_ascii=False)}",
        f"date: {when.strftime('%Y-%m-%d %H:%M:%S')}",
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        "---",
    ]
    return "\n".join(frontmatter) + "\n\n" + content.rstrip() + "\n"


def save_markdown(
    doc_type: str, title: str, source: str, content: str, when: datetime
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    markdown = build_markdown(doc_type, title, source, content, when)

    for _ in range(10):
        path = next_available_path(title, when)
        if not path.exists():
            atomic_write_text(path, markdown)
            return path
        time.sleep(0.2)

    raise RuntimeError(f"无法生成不重名的文件：{title}")


# =============================================================================
# 图片与合并转发
# =============================================================================
def save_image(msg: Any, chat: Any) -> str | None:
    """
    把图片消息落盘到 D:\\KnowledgeBase\\images\\，返回 Markdown 里使用的相对路径。

    wechatauto-replica 1.2.9 的接口：
        MediaDownloader(db, save_dir=...).download_image(user, local_id, save_dir=...)
    它直接从本地 media 库解密，不需要打开图片预览窗口。
    """
    # TODO: 请查阅 wechatauto-replica 文档确认 download_image 签名是否变化
    if DRY_RUN:
        LOG.info("[DRY-RUN] 将下载图片 local_id=%s（不写盘）", getattr(msg, "local_id", None))
        return f"./images/dry-run-{getattr(msg, 'local_id', 'x')}.jpg"

    db = getattr(chat, "_db", None)
    user = str(getattr(chat, "_wxid", "") or "")
    local_id = getattr(msg, "local_id", None)

    if db is None or not user or local_id is None:
        LOG.warning("图片消息缺少 db/user/local_id，无法下载（local_id=%s）", local_id)
        return None

    try:
        from wechatauto.media import MediaDownloader  # type: ignore[import-not-found]

        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        downloader = MediaDownloader(db, save_dir=str(IMAGES_DIR))
        saved = downloader.download_image(
            user, int(local_id), save_dir=str(IMAGES_DIR)
        )
    except Exception:
        LOG.exception("图片下载失败（local_id=%s）", local_id)
        return None

    if not saved:
        LOG.warning("图片未在本地找到（可能未加载原图，local_id=%s）", local_id)
        return None

    saved_path = Path(saved)
    # 正常情况下已经落在 images 目录；不在就复制一份，保证 Markdown 里的相对路径有效
    if saved_path.parent.resolve() != IMAGES_DIR.resolve():
        try:
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            target = IMAGES_DIR / saved_path.name
            if not target.exists():
                shutil.copy2(saved_path, target)
            saved_path = target
        except OSError:
            LOG.exception("复制图片到 %s 失败，保留原路径 %s", IMAGES_DIR, saved_path)

    return f"./images/{saved_path.name}"


_MERGED_FORWARD_MARKERS = ("recorditem", "<appmsg", "聊天记录")


def looks_like_merged_forward(content: Any) -> bool:
    """合并转发的聊天记录在库里是 appmsg XML，常见特征是 recorditem / 聊天记录。"""
    if not isinstance(content, str):
        return False
    if "recorditem" in content:
        return True
    if "<appmsg" in content and re.search(r"<type>\s*19\s*</type>", content):
        return True
    return "<appmsg" in content and "聊天记录" in content


def extract_merged_forward(content: str) -> str | None:
    """
    从 appmsg XML 里尽量提取纯文本。

    微信的合并转发大致结构：
        <appmsg ...>
          <title>群聊的聊天记录</title>
          <type>19</type>
          <recorditem><![CDATA[
             <recordinfo>...<dataitem datatype="1"><data>张三: 你好</data></dataitem>...
          ]]></recorditem>
        </appmsg>

    不同版本差异较大，所以这里多路兜底：data 标签 -> content 标签 -> title。
    全都取不到时返回 None，由调用方写入占位符，绝不丢消息。
    """
    if not isinstance(content, str) or not content.strip():
        return None

    text = content
    # 取出 CDATA 里的内容（没有 CDATA 就用整体）
    cdata = re.search(r"<recorditem>(.*?)</recorditem>", text, re.S)
    if cdata:
        text = cdata.group(1)

    items: list[str] = []
    # 1) <dataitem datatype="1"><data>文本</data></dataitem>
    for raw in re.findall(r"<data>(.*?)</data>", text, re.S):
        value = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if value:
            items.append(value)
    # 2) 普通 <content> 文本
    if not items:
        for raw in re.findall(r"<content>(.*?)</content>", text, re.S):
            value = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
            if value:
                items.append(value)

    title = ""
    title_match = re.search(r"<title>(.*?)</title>", content, re.S)
    if title_match:
        title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()

    lines: list[str] = []
    if title:
        lines.append(f"【合并转发】{title}")
    lines.extend(items)

    if not lines:
        return None
    return "\n".join(lines)


def extract_text(msg: Any) -> str | None:
    """
    把一条消息转成可归档文本：
      - 纯文本 / 引用回复：正文
      - 图片：下载后写 [图片: ./images/xxx.jpg]（失败写 [图片: 下载失败]）
      - 合并转发：提取文本，提取不到写 [合并转发的聊天记录]
      - 其它类型（语音/视频/文件/表情/系统）：返回 None
    """
    attr = str(getattr(msg, "attr", "") or "")
    if attr not in ("friend", "self"):
        return None
    if attr == "self" and not INCLUDE_SELF_MESSAGE:
        return None

    msg_type = str(getattr(msg, "type", "") or "")
    content = getattr(msg, "content", None)
    chat = getattr(getattr(msg, "parent", None), "root", None)  # 兜底，一般用不到

    # 1) 合并转发优先判断：它的 type 可能是 link/other/text 都说不定
    if looks_like_merged_forward(content):
        extracted = extract_merged_forward(content)
        return extracted if extracted else "[合并转发的聊天记录]"

    # 2) 图片
    if msg_type == "image":
        chat_obj = _chat_of(msg) or chat
        rel_path = save_image(msg, chat_obj) if chat_obj is not None else None
        return f"[图片: {rel_path}]" if rel_path else "[图片: 下载失败]"

    # 3) 普通文本 / 引用
    if msg_type not in TEXT_MESSAGE_TYPES:
        return None
    if not isinstance(content, str):
        return None

    text = content.strip()
    if not text or _PLACEHOLDER_PATTERN.match(text):
        return None
    return text


def _chat_of(msg: Any) -> Any | None:
    """
    取消息所属会话对象。

    Message 的 parent 是 _DBMessageParent，root 就是 Chat（见库源码）。
    TODO: 若新库改了这层结构，可改为在回调里显式把 chat 传下来
    """
    parent = getattr(msg, "parent", None)
    return getattr(parent, "root", None)


# =============================================================================
# 防抖合并器
# =============================================================================
@dataclass
class _PendingBuffer:
    parts: list[str] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)
    first_when: datetime = field(default_factory=datetime.now)
    timer: threading.Timer | None = None


class Debouncer:
    """
    同一联系人 10 秒内连续消息合并。

    - pending_msgs[chat] = {"text": 拼接文本, "fingerprints": [...], "timer": Timer}
    - 每来一条：取消旧定时器 -> 拼接 -> 起一个新定时器
    - 定时器到点：取出整批，交给 Collector 后台线程（AI + 落盘）
    - 全程加锁；close() 会把所有未落盘的 buffer 立即刷出，Ctrl+C 不丢消息
    """

    def __init__(self, collector: "Collector", delay: float = DEBOUNCE_SECONDS) -> None:
        self.collector = collector
        self.delay = delay
        self._buffers: dict[str, _PendingBuffer] = {}
        self._inflight: set[str] = set()  # 已进入 buffer 的指纹，防重复拼接
        self._lock = threading.Lock()
        self._closed = False

    def has(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._inflight

    def add(self, source: str, text: str, fingerprint: str) -> bool:
        """把一条消息拼进缓冲，并重置 10 秒定时器。"""
        when = datetime.now()

        with self._lock:
            if fingerprint in self._inflight:
                return False
            self._inflight.add(fingerprint)

            buffer = self._buffers.get(source)
            if buffer is None:
                buffer = _PendingBuffer(first_when=when)
                self._buffers[source] = buffer

            buffer.parts.append(text)
            buffer.fingerprints.append(fingerprint)

            if self._closed:
                # 正在退出：不再等定时器，立刻刷出
                self._buffers.pop(source, None)
                should_flush_now = True
            else:
                if buffer.timer is not None:
                    buffer.timer.cancel()
                timer = threading.Timer(self.delay, self._flush, args=(source,))
                timer.daemon = True
                buffer.timer = timer
                timer.start()
                should_flush_now = False

            snapshot = _PendingBuffer(
                parts=list(buffer.parts),
                fingerprints=list(buffer.fingerprints),
                first_when=buffer.first_when,
            )

        if should_flush_now:
            self._dispatch(source, snapshot)
        return True

    def _flush(self, source: str) -> None:
        with self._lock:
            buffer = self._buffers.pop(source, None)
            if buffer is None:
                return
            if buffer.timer is not None:
                buffer.timer.cancel()
            for fingerprint in buffer.fingerprints:
                self._inflight.discard(fingerprint)

        self._dispatch(source, buffer)

    def _dispatch(self, source: str, buffer: _PendingBuffer) -> None:
        text = "\n".join(part for part in buffer.parts if part).strip()
        if not text:
            return
        LOG.info(
            "防抖合并：%s 的 %d 条消息合并为 1 条（%d 字），准备落盘",
            source,
            len(buffer.fingerprints),
            len(text),
        )
        self.collector.submit_merged(
            source, text, tuple(buffer.fingerprints), buffer.first_when
        )

    def close(self) -> None:
        """退出前把所有缓冲立即刷出，避免丢消息。"""
        with self._lock:
            self._closed = True
            sources = list(self._buffers)

        for source in sources:
            self._flush(source)


# =============================================================================
# 后台处理器
# =============================================================================
class Collector:
    """AI + 落盘在后台单线程串行执行；同一个合并批次只写一个文件。"""

    def __init__(self, store: FingerprintStore, dry_run: bool = False) -> None:
        self.store = store
        self.dry_run = dry_run
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-worker")
        self.futures: set[Future] = set()
        self.accepted = 0
        self._lock = threading.Lock()

    def submit_merged(
        self,
        source: str,
        text: str,
        fingerprints: tuple[str, ...],
        when: datetime,
    ) -> bool:
        if not text.strip():
            return False

        self.accepted += 1
        future = self.executor.submit(self._process, source, text, fingerprints, when)
        with self._lock:
            self.futures.add(future)
        future.add_done_callback(lambda f: self._on_done(f))
        return True

    def _process(
        self,
        source: str,
        text: str,
        fingerprints: tuple[str, ...],
        when: datetime,
    ) -> None:
        # 防封：合并后、AI 请求前随机等待 2~5 秒（每批只等一次）
        time.sleep(random.uniform(*MESSAGE_SLEEP))

        result = classify_message(text)
        if result is None:
            doc_type, title = FALLBACK_TYPE, fallback_title(when)
        else:
            doc_type, title = result

        if self.dry_run:
            LOG.info(
                "[DRY-RUN] 将归档 [%s] %s <- %s（%d 条合并，不写文件）",
                doc_type,
                title,
                source,
                len(fingerprints),
            )
            return

        path = save_markdown(doc_type, title, source, text, when)
        # 先落盘再登记全部指纹：这些消息以后不会再被补处理重复归档
        for fingerprint in fingerprints:
            self.store.add(fingerprint, path.name, source, when)
        LOG.info("已归档 [%s] %s（%d 条消息合并）", doc_type, path, len(fingerprints))

    def _on_done(self, future: Future) -> None:
        with self._lock:
            self.futures.discard(future)
        try:
            future.result()
        except Exception:
            LOG.exception("后台处理失败，可等待下次补处理重试")

    def close(self) -> None:
        with self._lock:
            waiting = len(self.futures)
        if waiting:
            LOG.info("等待队列中 %d 个批次处理完成…", waiting)
        self.executor.shutdown(wait=True)

    @property
    def queued(self) -> int:
        with self._lock:
            return len(self.futures)


# =============================================================================
# 回调与监听
# =============================================================================
def make_callback(sink: Callable[[str, Any], bool]) -> Callable[[Any, Any], None]:
    """
    回调只做「取会话名 + 分类入缓冲」，不做耗时操作。
    防封等待已移到 Collector._process（合并后、AI 前）。
    """

    def on_message(msg: Any, chat: Any) -> None:
        try:
            # TODO: 确认 Chat 的显示名属性；当前用 AddListenChat 传入的名字最稳
            source = str(getattr(chat, "who", "") or "").strip() or "未知会话"
            sink(source, msg)
        except Exception:
            LOG.exception("监听回调处理消息失败（消息仍在本地库中，可被补处理）")

    return on_message


def make_sink(
    store: FingerprintStore, debouncer: Debouncer
) -> Callable[[str, Any], bool]:
    """消息 -> 文本/图片路径/合并转发文本 -> 指纹去重 -> 丢进防抖缓冲。"""

    def sink(source: str, msg: Any) -> bool:
        text = extract_text(msg)
        if not text:
            return False

        when = datetime.now()
        fingerprint = message_fingerprint(source, text, when)
        if fingerprint in store or debouncer.has(fingerprint):
            LOG.info("跳过重复消息：%s", text[:30])
            return False

        return debouncer.add(source, text, fingerprint)

    return sink


def add_listeners(wx: WeChat, callback: Callable[[Any, Any], None]) -> int:
    ok = 0
    for friend in TARGET_FRIENDS:
        try:
            result = wx.AddListenChat(friend, callback)
            if getattr(result, "is_success", True) is False:
                message = result.get("message") if isinstance(result, dict) else result
                LOG.error("监听 %s 失败：%s", friend, message)
                continue
            ok += 1
            LOG.info("开始监听：%s", friend)
        except Exception as exc:
            LOG.error("监听 %s 异常：%s", friend, exc, exc_info=True)
    return ok


def rebuild_listeners(wx: WeChat, callback: Callable[[Any, Any], None]) -> int:
    try:
        wx.StopListening(remove=True)
    except Exception:
        LOG.exception("停止旧监听失败")
    return add_listeners(wx, callback)


def listener_alive(wx: WeChat) -> bool:
    # TODO: 确认是否有公开的监听状态接口；当前读私有 _listener._thread
    listener = getattr(wx, "_listener", None)
    thread = getattr(listener, "_thread", None)
    if thread is None:
        return False
    return bool(thread.is_alive())


def find_chat(wx: WeChat, friend: str) -> Any | None:
    entry = (getattr(wx, "listen", {}) or {}).get(friend)
    if isinstance(entry, tuple):
        return entry[0]
    if entry is not None:
        return entry

    try:
        from wechatauto import Chat  # type: ignore[import-not-found]

        # TODO: 这里用了私有 _gui/_db；若新库提供公开构造方式请替换
        return Chat(friend, getattr(wx, "_gui", None), getattr(wx, "_db", None))
    except Exception:
        LOG.warning("构造会话 %s 失败，跳过补处理", friend, exc_info=True)
        return None


def message_timestamp(msg: Any) -> float | None:
    """TODO: 确认 create_time 单位；按秒处理，> 1e11 视为毫秒。"""
    value = getattr(msg, "create_time", None)
    if value is None:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e11:
        ts /= 1000.0
    return ts


def recover_pending(wx: WeChat, sink: Callable[[str, Any], bool]) -> int:
    """异常恢复：把最近窗口内未处理的消息重新灌回防抖缓冲。"""
    now = time.time()
    window = RECOVERY_WINDOW_MINUTES * 60
    total = 0

    for friend in TARGET_FRIENDS:
        chat = find_chat(wx, friend)
        if chat is None:
            continue
        try:
            messages = chat.GetAllMessage() or []
        except Exception:
            LOG.warning("补处理时读取会话 %s 失败，跳过", friend, exc_info=True)
            continue

        count = 0
        for msg in messages:
            ts = message_timestamp(msg)
            if ts is not None and now - ts > window:
                continue
            if sink(friend, msg):
                count += 1
        if count:
            total += count
            LOG.info("异常恢复：从会话 %s 补处理 %d 条消息", friend, count)

    return total


# =============================================================================
# 主循环与入口
# =============================================================================
def init_wechat() -> WeChat | None:
    for attempt in range(1, 4):
        try:
            LOG.info("正在初始化微信 4.x（第 %d/3 次，首次需解密本地库）", attempt)
            wx = WeChat()
            LOG.info("微信客户端初始化成功")
            return wx
        except Exception as exc:
            LOG.error("第 %d/3 次初始化失败：%s", attempt, exc)
            if attempt < 3:
                time.sleep(5.0)
    return None


def run(
    wx: WeChat,
    collector: Collector,
    identity: WeChatIdentity,
    callback: Callable[[Any, Any], None],
    sink: Callable[[str, Any], bool],
) -> None:
    """主线程保活：账号复核 + 监听健康检查 + 异常恢复。"""
    last_identity = time.monotonic()
    last_health = time.monotonic()

    while True:
        try:
            time.sleep(KEEPALIVE_SLEEP)
            now = time.monotonic()

            if now - last_identity >= IDENTITY_RECHECK_SECONDS:
                last_identity = now
                check_account_unchanged(wx, identity)

            if now - last_health >= LISTENER_HEALTH_CHECK_SECONDS:
                last_health = now
                if not listener_alive(wx):
                    LOG.error(
                        "监听线程已停止（可能因为微信窗口被关闭或切换了账号），"
                        "开始补处理并重建监听"
                    )
                    recover_pending(wx, sink)
                    if rebuild_listeners(wx, callback) == 0:
                        LOG.error("重建监听失败，%d 秒后重试", ERROR_SLEEP)
                        time.sleep(ERROR_SLEEP)

        except KeyboardInterrupt:
            raise
        except AccountSwitchedError:
            raise
        except Exception as exc:
            LOG.error(
                "监听循环异常（%s: %s，可能因为微信窗口被关闭或切换了账号）",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            try:
                recover_pending(wx, sink)
            except Exception:
                LOG.exception("异常恢复（补处理）失败")
            try:
                rebuild_listeners(wx, callback)
            except Exception:
                LOG.exception("重建监听失败")
            time.sleep(ERROR_SLEEP)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="微信 4.x 消息采集器 V2：防抖合并 + 图片归档 + 合并转发"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取、只分类并打印结果，不写 Markdown、图片与指纹",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global DRY_RUN

    args = parse_args(argv)
    DRY_RUN = bool(args.dry_run)

    prepare_stdout()
    setup_logging()

    LOG.info("=" * 64)
    LOG.info("微信消息采集器 V2 启动，输出目录：%s", OUTPUT_DIR)
    LOG.info("监听白名单：%s", "、".join(TARGET_FRIENDS) or "<空>")
    LOG.info("防抖窗口：%.0f 秒；图片目录：%s", DEBOUNCE_SECONDS, IMAGES_DIR)

    if args.dry_run:
        LOG.info("DRY-RUN 模式：只读不写")
    if not DEEPSEEK_API_KEY:
        LOG.warning("未检测到 DEEPSEEK_API_KEY，所有消息都会降级归档（待分类）")
    if not TARGET_FRIENDS:
        LOG.error("TARGET_FRIENDS 为空，请先在配置区填写要监听的联系人")
        return 1

    if EXPECTED_WECHAT_ID or EXPECTED_WECHAT_NICKNAME:
        LOG.info(
            "账号校验已开启：EXPECTED_WECHAT_ID=%r，EXPECTED_WECHAT_NICKNAME=%r",
            EXPECTED_WECHAT_ID,
            EXPECTED_WECHAT_NICKNAME,
        )
    else:
        LOG.warning("未配置账号校验，多人共用电脑时强烈建议填写 EXPECTED_WECHAT_*")

    print_startup_notice()

    if LEGACY_PROCESSED_FILE.exists():
        LOG.warning(
            "检测到旧版指纹文件 %s：算法已改为按天指纹，旧记录不再匹配，可删除",
            LEGACY_PROCESSED_FILE.name,
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    store = FingerprintStore(PROCESSED_FILE)
    LOG.info("已加载历史指纹 %d 条（指纹库：%s）", len(store), PROCESSED_FILE)

    collector = Collector(store, dry_run=args.dry_run)
    debouncer = Debouncer(collector, delay=DEBOUNCE_SECONDS)
    sink = make_sink(store, debouncer)
    callback = make_callback(sink)

    wx: WeChat | None = None
    try:
        wx = init_wechat()
        if wx is None:
            LOG.error("微信初始化失败，请确认 PC 微信 4.x 已登录后重新运行")
            return 1

        identity = read_identity(wx)
        print_identity_banner(identity)
        if not verify_identity(identity):
            LOG.error("账号校验未通过，已终止启动（未读取任何消息）")
            return 2

        if add_listeners(wx, callback) == 0:
            LOG.error("白名单内没有任何会话监听成功，请检查名称是否正确")
            return 1

        LOG.info("开始采集（10 秒防抖合并），按 Ctrl+C 停止")
        run(wx, collector, identity, callback, sink)
        return 0
    finally:
        # 先停监听，再把没到 10 秒的缓冲全部刷出，最后等队列写完
        if wx is not None:
            try:
                wx.StopListening(remove=True)
            except Exception:
                LOG.exception("停止监听失败")
        debouncer.close()
        collector.close()
        if args.dry_run:
            LOG.info("[DRY-RUN] 共接收 %d 个批次（未写入磁盘）", collector.accepted)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        LOG.info("收到 Ctrl+C，采集器已停止")
        sys.exit(0)
    except AccountSwitchedError as exc:
        LOG.critical("%s", exc)
        sys.exit(3)
    except Exception:
        LOG.exception("采集器异常退出")
        sys.exit(1)
