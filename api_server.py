# -*- coding: utf-8 -*-
"""
api_server.py —— 本地知识库 REST API（模块二）

职责：
    读取 D:\\KnowledgeBase 下所有 .md，解析 YAML Frontmatter + 正文，
    按 date 倒序返回 JSON 给前端 Vue3。

本次重构点：
    1. 编码兼容：优先 UTF-8(含 BOM)，失败自动回退 GBK（老工具写的文件不会凭空消失）；
    2. 内存缓存：以 (路径, mtime, size) 为键，文件没变就不重新解析 YAML；
    3. 缓存自动清理：文件被删除后，对应缓存条目同步移除。

接口：
    GET /api/knowledge    全部知识条目（数组，最新在前）
    GET /docs             FastAPI 自带交互文档

启动（项目根目录 D:\\CodeBuddyWorkSpace\\wechat）：
    .\\venv\\Scripts\\python.exe api_server.py
    或：.\\venv\\Scripts\\python.exe -m uvicorn api_server:app --host 127.0.0.1 --port 8000 --reload

依赖（若提示缺少 python-frontmatter）：
    .\\venv\\Scripts\\pip.exe install fastapi uvicorn python-frontmatter
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import socket
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        f"缺少依赖（{exc}），请执行：\n"
        r"    .\venv\Scripts\pip.exe install fastapi uvicorn"
    )

try:
    import frontmatter
except ImportError:  # pragma: no cover
    raise SystemExit(
        "缺少 python-frontmatter，请执行：\n"
        r"    .\venv\Scripts\pip.exe install python-frontmatter"
    )

try:
    import status_bus
except ImportError:
    status_bus = None


# =============================================================================
# 配置区
# =============================================================================
PROJECT_ROOT = Path(__file__).resolve().parent


def _load_local_env() -> None:
    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_local_env()
LOCAL_API_TOKEN = os.environ.get("LOCAL_API_TOKEN", "").strip()
ALLOWED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]

KNOWLEDGE_DIR = Path(r"D:\KnowledgeBase")   # 知识库目录（模块一生成的 Markdown）
FRONTEND_DIST = Path(
    os.environ.get("KB_FRONTEND_DIST", r"C:\dev\notice-to-calendar\dist")
)  # Vue 构建产物；可用 KB_FRONTEND_DIST 覆盖
SERVER_HOST = "127.0.0.1"                   # 本机调试；局域网访问可改 "0.0.0.0"
SERVER_PORT = 8000
DEFAULT_TYPE = "knowledge"                  # Frontmatter 缺 type 时的兜底值

logger = logging.getLogger("kb_api")


def _force_utf8_console() -> None:
    """把控制台切到 UTF-8：Windows 默认 GBK，中文日志/报错会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_console()  # 必须在导入第三方库、抛出中文报错之前执行


# =============================================================================
# 数据模型
# =============================================================================
class KnowledgeItem(BaseModel):
    filename: str        # 2026-11-02-跟张总开会.md
    type: str            # schedule | knowledge
    title: str
    source: str          # 微信联系人
    date: str            # YYYY-MM-DD HH:MM:SS
    tags: list[str]
    content: str
    category: str = ""
    is_merged: bool = False
    merged_into: str = ""
    merged_from: list[str] = []


class ControlCommand(BaseModel):
    command: str


class MergeRequest(BaseModel):
    filenames: list[str]
    title: str = ""
    type: str = "knowledge"
    tags: list[str] = []
    category: str = ""


class CategoryRequest(BaseModel):
    filename: str
    category: str


class LinkRequest(BaseModel):
    url: str
    title: str = ""


# =============================================================================
# 解析工具
# =============================================================================
_FILENAME_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_FILENAME_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")
_FILENAME_SUFFIX = re.compile(r"-\d+$")
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
)


def coerce_datetime(value: Any) -> datetime | None:
    """
    把 Frontmatter 的 date 统一成 naive datetime。

    PyYAML 可能给出 datetime / date，手写的可能是字符串，还可能是带时区的 ISO。
    带时区的一律去掉 tzinfo，避免 aware 与 naive 混合比较抛 TypeError。
    """
    if isinstance(value, datetime):
        dt: datetime | None = value
    elif isinstance(value, date):  # datetime 是 date 子类，必须后判断
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        dt = None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in _DATE_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
    else:
        return None

    if dt is None:
        return None
    return dt.replace(tzinfo=None)


def date_from_filename(path: Path) -> datetime | None:
    match = _FILENAME_DATE.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d")
    except ValueError:
        return None


def resolve_date(raw: Any, path: Path) -> tuple[str, datetime]:
    """展示用日期 + 排序用时间；兜底顺序：Frontmatter -> 文件名 -> 文件 mtime。"""
    dt = coerce_datetime(raw) or date_from_filename(path)
    if dt is None:
        dt = datetime.fromtimestamp(path.stat().st_mtime)
    return dt.strftime("%Y-%m-%d %H:%M:%S"), dt


def title_from_filename(path: Path) -> str:
    title = _FILENAME_SUFFIX.sub("", _FILENAME_PREFIX.sub("", path.stem)).strip()
    return title or path.stem


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    return text or default


def normalize_tags(value: Any) -> list[str]:
    """数组原样；"a, b" 按逗号/分号拆分；None -> []。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts: Iterable[Any] = re.split(r"[,，;；]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = value
    else:
        parts = [value]

    tags: list[str] = []
    for part in parts:
        text = as_text(part)
        if text and text not in tags:
            tags.append(text)
    return tags


def read_markdown(path: Path) -> str:
    """
    读取 Markdown 文本。

    先按 UTF-8(utf-8-sig 可吞掉 BOM) 解码；失败再尝试 GBK，
    避免用记事本等工具保存的旧文件因为编码问题被永久跳过。
    """
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        logger.warning("%s 不是 UTF-8 编码，已按 GBK 读取", path.name)
        return raw.decode("gbk", errors="replace")


def load_item(path: Path) -> tuple[datetime, KnowledgeItem]:
    """
    解析单个 Markdown，返回 (排序时间, 数据对象)。

    YAML 语法错误、缺少 Frontmatter、正文为空等都会抛异常，
    由 load_all_items 逐文件捕获，坏文件不会影响整个接口。
    """
    text = read_markdown(path)
    post = frontmatter.loads(text)

    meta = post.metadata or {}
    if not isinstance(meta, dict):
        raise ValueError("Frontmatter 不是键值对结构")
    if not meta:
        raise ValueError("缺少 YAML Frontmatter")

    content = (post.content or "").strip()
    if not content:
        raise ValueError("正文为空")

    date_text, sort_dt = resolve_date(meta.get("date"), path)
    item = KnowledgeItem(
        filename=path.name,
        type=as_text(meta.get("type"), DEFAULT_TYPE),
        title=as_text(meta.get("title")) or title_from_filename(path),
        source=as_text(meta.get("source")),
        date=date_text,
        tags=normalize_tags(meta.get("tags")),
        content=content,
        category=as_text(meta.get("category")),
        is_merged=bool(meta.get("is_merged", False)),
        merged_into=as_text(meta.get("merged_into")),
        merged_from=normalize_tags(meta.get("merged_from")),
    )
    return sort_dt, item


# =============================================================================
# 内存缓存：(路径, mtime, size) 不变就不重新解析
# =============================================================================
_CACHE: dict[str, tuple[tuple[float, int], tuple[datetime, KnowledgeItem]]] = {}
_CACHE_LOCK = threading.Lock()


def load_item_cached(path: Path) -> tuple[datetime, KnowledgeItem]:
    """带缓存的解析：文件修改时间与大小都没变，直接返回上次结果。"""
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"无法读取文件信息：{exc}") from exc

    stamp = (stat.st_mtime, stat.st_size)
    key = str(path)

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    parsed = load_item(path)

    with _CACHE_LOCK:
        _CACHE[key] = (stamp, parsed)
    return parsed


def prune_cache(existing_keys: set[str]) -> None:
    """删除已经不存在的文件对应的缓存，防止长期运行时内存只增不减。"""
    with _CACHE_LOCK:
        for key in [k for k in _CACHE if k not in existing_keys]:
            _CACHE.pop(key, None)


def load_all_items() -> list[KnowledgeItem]:
    """扫描知识库目录，解析全部 .md（走缓存）并按日期倒序返回。"""
    if not KNOWLEDGE_DIR.exists():
        logger.warning("知识库目录不存在：%s", KNOWLEDGE_DIR)
        return []

    records: list[tuple[datetime, KnowledgeItem]] = []
    existing: set[str] = set()
    skipped = 0

    for path in KNOWLEDGE_DIR.glob("*.md"):
        if not path.is_file():
            continue
        existing.add(str(path))
        try:
            records.append(load_item_cached(path))
        except Exception as exc:
            # 单个文件有问题只记录告警，绝不让整个请求 500
            skipped += 1
            logger.warning(
                "跳过无法解析的文件：%s（%s: %s）", path.name, type(exc).__name__, exc
            )

    prune_cache(existing)

    records.sort(key=lambda pair: (pair[0], pair[1].filename), reverse=True)
    logger.debug("共解析 %d 条，跳过 %d 条异常文件", len(records), skipped)
    return [item for _, item in records]


# =============================================================================
# FastAPI 应用
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("知识库目录：%s（存在：%s）", KNOWLEDGE_DIR, KNOWLEDGE_DIR.exists())
    logger.info("接口地址：http://%s:%d/api/knowledge", SERVER_HOST, SERVER_PORT)
    logger.info("接口文档：http://%s:%d/docs", SERVER_HOST, SERVER_PORT)
    yield
    logger.info("服务已关闭")


app = FastAPI(
    title="微信知识库 API",
    description="读取本地 Markdown 知识库，向前端提供结构化 JSON 数据",
    version="1.1.0",
    lifespan=lifespan,
)

# 写操作必须带本地 Token。放在 CORS 之前注册，让 CORS 成为外层，
# 这样 401 响应也会带 Access-Control-Allow-Origin，前端能看到真实错误。
@app.middleware("http")
async def require_local_token(request, call_next):
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path.startswith("/api/")
    ):
        if not LOCAL_API_TOKEN:
            return JSONResponse(
                status_code=503,
                content={"detail": "LOCAL_API_TOKEN 未配置，写操作已禁用"},
            )
        provided = request.headers.get("authorization", "")
        expected = f"Bearer {LOCAL_API_TOKEN}"
        if not hmac.compare_digest(provided, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "本地 API Token 无效"},
            )
    return await call_next(request)


# CORS 只允许本机前端；不再使用 "*"，避免任意网页读写本地知识库。
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)


def _port_open(port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _tray_online(data: dict[str, Any]) -> bool:
    if not data.get("running"):
        return False
    updated = str(data.get("updated_at") or "")
    if not updated:
        return False
    try:
        stamp = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (datetime.now() - stamp).total_seconds() <= 8


@app.get("/api/health", summary="本地服务健康检查")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "wechat-kb-api"}


@app.get("/api/status", summary="采集器、托盘、前端运行状态")
def get_status() -> dict[str, Any]:
    data = status_bus.read_status() if status_bus is not None else {}
    data = dict(data or {})
    data["api_online"] = True
    data["tray_online"] = _tray_online(data)
    data["frontend_online"] = FRONTEND_DIST.is_dir() or _port_open(5173)
    data["frontend_url"] = "/console"
    data["server_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return data


@app.post("/api/control", summary="向托盘发送扫描/暂停命令")
def control(cmd: ControlCommand) -> dict[str, Any]:
    if status_bus is None:
        raise HTTPException(status_code=503, detail="status_bus 不可用")
    command = (cmd.command or "").strip()
    allowed = {"scan", "toggle_pause", "open_panel"}
    if command not in allowed:
        raise HTTPException(status_code=400, detail=f"不支持的命令：{command}")

    if command == "open_panel":
        panel = Path(__file__).with_name("status_panel.py")
        subprocess.Popen(
            [sys.executable, str(panel)],
            cwd=str(Path(__file__).parent),
            close_fds=True,
        )
    else:
        status_bus.send_command(command)
    return {"ok": True, "command": command}


def _knowledge_path(filename: str) -> Path:
    path = (KNOWLEDGE_DIR / filename).resolve()
    try:
        path.relative_to(KNOWLEDGE_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法文件名：{filename}")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{filename}")
    return path


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _write_post(path: Path, post: frontmatter.Post) -> None:
    _write_text_atomic(path, frontmatter.dumps(post) + "\n")


@app.post("/api/knowledge/category", response_model=KnowledgeItem, summary="把分类写回 Markdown")
def set_category(req: CategoryRequest) -> KnowledgeItem:
    path = _knowledge_path(req.filename)
    category = (req.category or "").strip() or "未分类"
    post = frontmatter.loads(read_markdown(path))
    post["category"] = category
    _write_post(path, post)
    _, item = load_item(path)
    return item


@app.post("/api/knowledge/link", response_model=KnowledgeItem, summary="把链接存成知识卡片")
def add_link(req: LinkRequest) -> KnowledgeItem:
    url = (req.url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise HTTPException(status_code=400, detail="只支持 http/https 链接")

    # 同一个 URL 只保留一张卡，避免重复粘贴生成一堆重复条目
    for existing in load_all_items():
        if url in existing.content:
            return existing

    title = (req.title or "").strip() or url
    body = f"[{title}]({url})\n\n{url}"
    now = datetime.now()
    post = frontmatter.Post(
        body,
        type="knowledge",
        title=title,
        source="链接",
        date=now.strftime("%Y-%m-%d %H:%M:%S"),
        tags=["链接"],
        category="未分类",
    )

    safe_title = re.sub(r'[\\/:*?"<>|]+', "-", title).strip() or "链接"
    stem = f"{now:%Y-%m-%d}-{safe_title[:60]}"
    target = KNOWLEDGE_DIR / f"{stem}.md"
    index = 2
    while target.exists():
        target = KNOWLEDGE_DIR / f"{stem}-{index}.md"
        index += 1
    _write_post(target, post)
    _, created = load_item(target)
    return created


@app.post("/api/knowledge/merge", response_model=KnowledgeItem, summary="合并多张知识卡片")
def merge_knowledge(req: MergeRequest) -> KnowledgeItem:
    if len(req.filenames) < 2:
        raise HTTPException(status_code=400, detail="至少选择两张卡片")

    items: list[KnowledgeItem] = []
    paths: list[Path] = []
    for name in req.filenames:
        path = _knowledge_path(name)
        _, item = load_item(path)
        paths.append(path)
        items.append(item)

    already_merged = [
        item.filename for item in items if item.is_merged or item.merged_into
    ]
    if already_merged:
        raise HTTPException(
            status_code=409,
            detail=(
                "以下卡片已经合并过，不能再次合并："
                + "、".join(already_merged)
            ),
        )

    title = (req.title or "").strip() or f"合并知识-{datetime.now():%Y%m%d-%H%M}"
    doc_type = req.type if req.type in {"schedule", "knowledge"} else "knowledge"
    tags = list(dict.fromkeys([*req.tags, "合并"]))
    category = (req.category or "").strip() or "未分类"

    body_parts = []
    for item in items:
        body_parts.append(f"## {item.title}\n\n{item.content}")
    body = "\n\n---\n\n".join(body_parts)

    post = frontmatter.Post(
        body,
        type=doc_type,
        title=title,
        source="合并",
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        tags=tags,
        category=category,
        merged_from=[item.filename for item in items],
    )

    safe_title = re.sub(r'[\\/:*?"<>|]+', "-", title).strip() or "合并知识"
    stem = f"{datetime.now():%Y-%m-%d}-{safe_title}"
    target = KNOWLEDGE_DIR / f"{stem}.md"
    index = 2
    while target.exists():
        target = KNOWLEDGE_DIR / f"{stem}-{index}.md"
        index += 1

    # 先备份原文件原文，标记失败时整体回滚
    backups = {path: read_markdown(path) for path in paths}
    _write_post(target, post)

    try:
        for path in paths:
            original = frontmatter.loads(backups[path])
            original["is_merged"] = True
            original["merged_into"] = target.name
            _write_post(path, original)
    except Exception as exc:
        logger.exception("合并回滚：标记原卡片失败")
        for path, text in backups.items():
            try:
                _write_text_atomic(path, text)
            except Exception:
                logger.exception("恢复原卡片失败：%s", path.name)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            logger.exception("删除未完成合并卡片失败：%s", target.name)
        raise HTTPException(status_code=500, detail="合并失败，已回滚") from exc

    _, created = load_item(target)
    return created


@app.post("/api/open/{target}", summary="打开日志或知识库目录")
def open_target(target: str) -> dict[str, Any]:
    if target == "log":
        path = KNOWLEDGE_DIR / "tray_app.log"
    elif target == "folder":
        path = KNOWLEDGE_DIR
    else:
        raise HTTPException(status_code=400, detail="target 只能是 log 或 folder")
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"不存在：{path}")
    os.startfile(str(path))  # type: ignore[attr-defined]
    return {"ok": True, "path": str(path)}


@app.get(
    "/api/knowledge",
    response_model=list[KnowledgeItem],
    summary="获取全部知识条目（按日期倒序）",
)
def get_knowledge() -> list[KnowledgeItem]:
    """
    读取 D:\\KnowledgeBase 下全部 .md 并返回数组。

    FastAPI 默认用 UTF-8 序列化 JSON（ensure_ascii=False），中文不会被转义。
    """
    return load_all_items()


# =============================================================================
# Vue 构建产物（可选）：dist 存在时，直接由 8000 端口提供 /console、/knowledge
# =============================================================================
_images = KNOWLEDGE_DIR / "images"
if _images.is_dir():
    app.mount("/images", StaticFiles(directory=str(_images)), name="images")

if FRONTEND_DIST.is_dir():
    _assets = FRONTEND_DIST / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str):
        if full_path.startswith("api/") or full_path in {
            "docs",
            "redoc",
            "openapi.json",
        }:
            raise HTTPException(status_code=404, detail="Not Found")

        root = FRONTEND_DIST.resolve()
        candidate = (root / full_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        index = root / "index.html"
        if index.is_file():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="前端构建产物不存在")


# =============================================================================
# 启动入口
# =============================================================================
def setup_logging() -> None:
    _force_utf8_console()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


if __name__ == "__main__":
    setup_logging()
    uvicorn.run(
        "api_server:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=False,              # 常驻服务不启 reload，避免 pythonw 下多进程
        log_level="info",
    )
