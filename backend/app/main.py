"""MGUS House 竞赛系统 — FastAPI 入口。

生产启动（§17.1）：uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
"""
import asyncio
import hashlib
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.base import Base
from app.db.session import get_engine, make_session_factory
from app.modules.accounts.router import router as accounts_router
from app.modules.audit.router import router as audit_router
from app.modules.competitions.controller import controller
from app.modules.competitions.router import router as competitions_router
from app.modules.houses.router import router as houses_router
from app.modules.practice.router import router as practice_router
from app.modules.practice.leaderboard import router as leaderboard_router
from app.modules.questions.router import router as questions_router
from app.modules.scoring.router import router as scoring_router
from app.modules.submissions.router import router as submissions_router
from app.modules.assistance.router import router as assistance_router
from app.realtime.router import router as ws_router
from app.realtime.tasks import progress_broadcaster

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mgus.app")

_broadcaster_stop: asyncio.Event | None = None
_broadcaster_task: asyncio.Task | None = None

# 实例指纹（见 /healthz）：进程启动时刻 + app/ 源码摘要，用于分辨"到底是谁在接单"。
_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
_REVISION_CACHE: dict[str, str] = {}

# ── 单写者守卫（B1-E4）──────────────────────────────────────────────────────
# 权威状态分布在数据库；但「连接 Hub / 限流桶 / 在途计数 / 比赛控制器」都是进程内的。
# 多 worker 或多副本会让它们各自为政：进度只广播给连到本副本的人、限流各算各的、
# 控制器可能同时推进同一场比赛。首版因此**硬性要求单进程**，并在启动时直接拒绝。
# 水平扩容路径（Redis pub/sub + 独立广播进程）属于 v2，不在首版做。


def _assert_single_writer() -> None:
    raw = (os.environ.get("WEB_CONCURRENCY") or os.environ.get("UVICORN_WORKERS")
           or os.environ.get("MGUS_WORKERS") or "").strip()
    try:
        workers = int(raw) if raw else 1
    except ValueError:
        workers = 1
    if workers > 1:
        raise RuntimeError(
            f"检测到 workers={workers}，但本版本只支持单进程运行："
            "实时连接 Hub、限流计数、在途提交计数与比赛控制器均驻留在进程内，"
            "多 worker 会导致进度广播丢失与状态不一致。"
            "请使用 uvicorn --workers 1（默认），或先按 docs/production-plan.md 完成 Redis 化改造。"
        )
    if os.environ.get("MGUS_REPLICA_ID"):
        log.warning("MGUS_REPLICA_ID 已设置；首版不支持多副本，请确认只有单实例在运行。")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _broadcaster_stop, _broadcaster_task
    settings = get_settings()
    _assert_single_writer()   # 多 worker 直接拒绝启动（B1-E4）
    # 开发/测试便捷建表；生产经 Alembic 迁移（MGUS_SKIP_DDL=1 关闭）
    if not os.environ.get("MGUS_SKIP_DDL"):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    await controller.start()
    _broadcaster_stop = asyncio.Event()
    _broadcaster_task = asyncio.create_task(progress_broadcaster(_broadcaster_stop))
    log.info("MGUS backend started (recovered_matches=%s)", controller.recovered_matches)
    yield
    _broadcaster_stop.set()
    if _broadcaster_task:
        await _broadcaster_task
    await controller.stop()


app = FastAPI(title="MGUS House Quiz API", version="0.1.0", lifespan=lifespan)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    request.state.request_id = rid
    try:
        response = await call_next(request)
    except AppError as e:
        response = JSONResponse(
            status_code=e.status,
            content={"code": e.code, "message": e.message,
                     "request_id": rid, "details": e.details})
    except Exception:
        log.exception("unhandled error")
        response = JSONResponse(
            status_code=500,
            content={"code": "INTERNAL_ERROR", "message": "服务器内部错误。",
                     "request_id": rid, "details": {}})
    response.headers["X-Request-Id"] = rid
    return response


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """CSRF 双提交令牌校验（B1-E5）。

    规则：Cookie 会话下的所有**非安全方法**必须带 `X-CSRF-Token`，其值等于服务端签发的
    会话明文（仅存于 HttpOnly Cookie 与同一站点可读的 `mgus_csrf` Cookie）。
    跨站页面既能读 Cookie 也能设 Cookie，但**无法知道**会话明文，因此无法伪造该头。

    另加 Origin 白名单作为第二道：即使令牌泄露，来自未登记来源的写请求仍被拒。
    """
    settings = get_settings()
    method = request.method.upper()
    if method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and settings.allowed_origins and origin not in settings.allowed_origins:
            return _csrf_reject(request, "请求来源未被允许。")
        session_cookie = request.cookies.get(settings.cookie_name)
        header_token = request.headers.get(settings.csrf_header_name)
        if session_cookie and not header_token:
            return _csrf_reject(request, "缺少 CSRF 令牌。请刷新页面后重试。")
        if session_cookie and header_token and not secrets.compare_digest(session_cookie, header_token):
            return _csrf_reject(request, "CSRF 令牌不匹配。请刷新页面后重试。")
    return await call_next(request)


def _csrf_reject(request: Request, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"code": ErrorCode.CSRF_FAILED, "message": message,
                 "request_id": getattr(request.state, "request_id", None), "details": {}},
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status,
                        content={"code": exc.code, "message": exc.message,
                                 "request_id": getattr(request.state, "request_id", None),
                                 "details": exc.details})


app.include_router(accounts_router)
app.include_router(houses_router)
app.include_router(questions_router)
app.include_router(practice_router)
app.include_router(leaderboard_router)
app.include_router(competitions_router)
app.include_router(submissions_router)
app.include_router(assistance_router)
app.include_router(scoring_router)
app.include_router(audit_router)
app.include_router(ws_router)


def _instance_stamp() -> dict:
    return {"pid": os.getpid(), "started_at": _STARTED_AT, "revision": _app_revision()}


def _app_revision() -> str:
    """app/ 下所有 .py 内容摘要（前 12 位）。只算一次，进程内缓存。"""
    if "rev" in _REVISION_CACHE:
        return _REVISION_CACHE["rev"]
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    try:
        for p in sorted(root.rglob("*.py")):
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
        rev = h.hexdigest()[:12]
    except OSError:
        rev = "unknown"
    _REVISION_CACHE["rev"] = rev
    return rev


@app.get("/healthz")
async def healthz():
    """健康检查 + **实例指纹**。

    `revision` 是 app/ 源码目录的内容摘要，`started_at` 是本进程启动时刻。
    加它们是因为在 Windows 上踩过一个很隐蔽的坑：多个进程可以同时 bind 同一端口
    （没有 SO_EXCLUSIVEADDRUSE），于是**旧进程继续接单**、页面表现像是"改了没生效"。
    现在只要比对 healthz 的 revision/pid，就能立刻分辨响应来自哪个实例。
    """
    return {"ok": True, "controller_alive": controller.alive, **_instance_stamp()}


@app.get("/readyz")
async def readyz():
    checks = {"controller": controller.alive}
    try:
        factory = make_session_factory()
        async with factory() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False
    ok = all(checks.values())
    return JSONResponse(status_code=200 if ok else 503,
                        content={"ok": ok, "checks": checks, **_instance_stamp()})
