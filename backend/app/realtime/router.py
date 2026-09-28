"""WebSocket 入口（§14.2）。

- 同源 Cookie 鉴权 + Origin 校验；服务端决定订阅范围（匿名仅公开进度）。
- 连接只负责通知；权威状态始终可经 HTTP 快照恢复（§14.3）。
"""
import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import token_hash
from app.db.base import as_utc, utcnow
from app.db.models import User, UserSession
from app.db.session import make_session_factory
from app.realtime.hub import Conn, hub
from app.realtime import protocol

log = logging.getLogger("mgus.ws")
router = APIRouter()


async def _resolve_user(ws: WebSocket):
    token = ws.cookies.get(get_settings().cookie_name)
    if not token:
        return None
    factory = make_session_factory()
    async with factory() as db:
        sess = (await db.execute(select(UserSession).where(
            UserSession.token_hash == token_hash(token)))).scalar_one_or_none()
        if sess is None or sess.revoked_at is not None or \
                as_utc(sess.expires_at) < as_utc(utcnow()):
            return None
        user = await db.get(User, sess.user_id)
        if user is None or user.status != "active" or user.active_session_id != sess.id:
            return None
        return {"id": user.id, "role": user.role, "house_id": user.house_id}


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, match: str | None = Query(default=None)):
    origin = ws.headers.get("origin")
    settings = get_settings()
    if origin and settings.allowed_origins and origin not in settings.allowed_origins:
        await ws.close(code=4403)
        return

    await ws.accept()
    ident = await _resolve_user(ws)

    entry_id = None
    house_key = None
    if ident is not None and match:
        factory = make_session_factory()
        async with factory() as db:
            from app.db.models import MatchEntry, House
            e = (await db.execute(select(MatchEntry).where(
                MatchEntry.match_id == match, MatchEntry.user_id == ident["id"]))).scalar_one_or_none()
            if e is not None:
                entry_id = e.id
                house_key = e.house_key_snapshot
            elif ident["house_id"]:
                h = await db.get(House, ident["house_id"])
                house_key = h.key if h else None

    conn = Conn(ws=ws, user_id=ident["id"] if ident else None,
                role=ident["role"] if ident else None, match_id=match)
    conn.entry_id = entry_id          # 动态字段：进度广播个性化用
    conn.house_key = house_key
    hub.conns.add(conn)

    async def sender() -> None:
        while True:
            msg = await conn.queue.get()
            await ws.send_json(msg)

    send_task = asyncio.create_task(sender())
    try:
        if match:
            factory = make_session_factory()
            async with factory() as db:
                m = await db.get(__import__("app.db.models", fromlist=["Match"]).Match, match)
                if m is not None:
                    hub.send(conn, protocol.message(
                        protocol.EV_MATCH_STATE, match,
                        {"status": m.status, "state_version": m.state_version,
                         "server_time": protocol.server_time_iso()},
                        state_version=m.state_version))
        while True:
            raw = await ws.receive_text()
            if raw == "ping":
                hub.send(conn, protocol.message("pong", match, {}))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws error user=%s match=%s", ident and ident["id"], match)
    finally:
        hub.conns.discard(conn)
        send_task.cancel()
