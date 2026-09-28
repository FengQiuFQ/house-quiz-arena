"""比赛控制器（应用生命周期内启动，§12.2）。

- 单进程 tick 驱动：推进回合、发布到期揭晓、重启恢复。
- 权威状态持久在数据库；控制器崩溃只影响节奏推进，不丢数据（§10.3）。
"""
import asyncio
import logging

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import make_session_factory
from app.db.models import Match, MATCH_RUNNING, MATCH_REVEAL_SCHEDULED
from app.modules.competitions.service import advance_match, recover_on_startup
from app.realtime.hub import hub

log = logging.getLogger("mgus.controller")


class MatchController:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_tick_error: str | None = None
        self.recovered_matches: int = 0
        self.ticks: int = 0

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        factory = make_session_factory()
        async with factory() as db:
            self.recovered_matches = await recover_on_startup(db)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="mgus-match-controller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = get_settings().round_tick_s
        while not self._stop.is_set():
            try:
                await self.tick_all()
                self.last_tick_error = None
            except Exception as exc:  # 单次 tick 异常不终止控制器（§12.2）
                self.last_tick_error = f"{type(exc).__name__}: {exc}"
                log.exception("controller tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def tick_all(self) -> None:
        self.ticks += 1
        factory = make_session_factory()
        async with factory() as db:
            matches = (await db.execute(
                select(Match).where(Match.status.in_([MATCH_RUNNING, MATCH_REVEAL_SCHEDULED]))
            )).scalars().all()
            for match in matches:
                broadcasts = await advance_match(db, match)
                await db.commit()
                for b in broadcasts:
                    hub.broadcast(b["match_id"], _mk(b))


def _mk(b: dict) -> dict:
    from app.realtime import protocol
    return protocol.message(b["type"], b["match_id"], b["payload"],
                            state_version=b.get("state_version"))


controller = MatchController()
