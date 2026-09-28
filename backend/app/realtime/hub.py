"""进程内连接 Hub。

- 每连接有界发送队列（默认 64 帧）；慢客户端丢帧而不是无限缓存（§10.5）。
- 进度快照是完整可替换状态，丢帧后客户端以 HTTP 快照/下一帧自愈（§14.3）。
- 订阅范围由服务端决定：匿名仅公开进度；选手含个人状态；智囊含本 House 援助（§14.2）。
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

from app.realtime import protocol

log = logging.getLogger("mgus.hub")

QUEUE_LIMIT = 64


@dataclass(eq=False)
class Conn:
    """连接记录。eq=False：身份即对象本身，否则 set 成员需要可散列字段而默认 dataclass 不可散列。"""

    ws: object
    user_id: str | None
    role: str | None            # admin/entrant/advisor/student/None(匿名)
    match_id: str | None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_LIMIT))
    dropped: int = 0


class Hub:
    def __init__(self) -> None:
        self.conns: set[Conn] = set()
        self.progress_seq: dict[str, int] = {}

    def next_seq(self, match_id: str) -> int:
        self.progress_seq[match_id] = self.progress_seq.get(match_id, 0) + 1
        return self.progress_seq[match_id]

    def reset_stream(self, match_id: str) -> None:
        """比赛阶段跳变时允许进度流重新起序（§14.2）。"""
        self.progress_seq.pop(match_id, None)

    def send(self, conn: Conn, message: dict) -> None:
        """同步入队，绝不阻塞、绝不派生任务（§10.5）。

        每个连接一个 Task 的写法在 200+ 连接时会变成每秒数百个 Task 抢占事件循环，
        而队列本身就是有界的（QUEUE_LIMIT），满则丢帧正是设计意图；这里因此保持同步。
        """
        try:
            conn.queue.put_nowait(message)
        except asyncio.QueueFull:
            conn.dropped += 1
            if conn.dropped == 1 or conn.dropped % 50 == 0:
                log.warning("slow client dropped=%s user=%s", conn.dropped, conn.user_id)

    def broadcast(self, match_id: str | None, message: dict,
                  predicate: Callable[[Conn], bool] | None = None) -> None:
        """在事件循环内调用；对每个匹配连接投递一帧（入队即返回，不派生 Task）。"""
        for conn in list(self.conns):
            if match_id is not None and conn.match_id != match_id:
                continue
            if predicate is not None and not predicate(conn):
                continue
            self.send(conn, message)

    def connected_ids(self, match_id: str) -> int:
        return sum(1 for c in self.conns if c.match_id == match_id)


hub = Hub()
