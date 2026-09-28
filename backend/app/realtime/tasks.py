"""后台广播任务（§10.5）：每秒一次批量进度快照，个性化补丁在发送侧合并。"""
import asyncio
import logging

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Match
from app.db.session import make_session_factory
from app.modules.competitions.snapshot import _progress
from app.realtime import protocol
from app.realtime.hub import hub

log = logging.getLogger("mgus.broadcaster")


async def progress_broadcaster(stop: asyncio.Event) -> None:
    interval = get_settings().broadcast_interval_s
    while not stop.is_set():
        try:
            await _tick()
        except Exception:
            log.exception("progress tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _tick() -> None:
    match_ids = {c.match_id for c in hub.conns if c.match_id}
    if not match_ids:
        return
    factory = make_session_factory()
    for mid in match_ids:
        async with factory() as db:
            m = await db.get(Match, mid)
            if m is None or m.status in ("draft", "cancelled", "archived"):
                continue
            # 已交名单只在 individual 档才查（默认 house 档不付这个成本，§8）
            want_ids = getattr(m, "live_visibility", "house") == "individual"
            box: dict[str, set[str]] = {}
            progress = await _progress(db, m, box if want_ids else None)
            progress["connections"] = hub.connected_ids(mid)
            state_version = m.state_version
            submitted_ids = box.get("ids", set()) if want_ids else set()

        seq = hub.next_seq(mid)
        for conn in list(hub.conns):
            if conn.match_id != mid:
                continue
            payload = dict(progress)
            if conn.entry_id is not None:
                payload["you"] = {"submitted": conn.entry_id in submitted_ids}
            hub.send(conn, protocol.message(
                protocol.EV_PROGRESS, mid, payload,
                state_version=state_version, progress_seq=seq))
