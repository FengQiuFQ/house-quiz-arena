"""WebSocket 消息构造（§14.2）。

所有消息统一：protocol_version / type / match_id / server_time / state_version / payload。
进度快照带 stream_id + progress_seq，断线重连允许重新起序（§14.2）。
"""
from app.core.config import get_settings
from app.db.base import server_time_iso

PROTOCOL_VERSION = get_settings().protocol_version


def message(type_: str, match_id: str | None, payload: dict, state_version: int | None = None,
            progress_seq: int | None = None) -> dict:
    m = {
        "protocol_version": PROTOCOL_VERSION,
        "type": type_,
        "match_id": match_id,
        "server_time": server_time_iso(),
        "payload": payload,
    }
    if state_version is not None:
        m["state_version"] = state_version
    if progress_seq is not None:
        m["progress_seq"] = progress_seq
    return m


# 事件类型常量
EV_MATCH_STATE = "match.state"
EV_ROUND_OPENED = "round.opened"
EV_ROUND_CLOSED = "round.closed"
EV_PROGRESS = "progress.snapshot"
EV_AID_UPDATED = "aid.updated"
EV_SESSION_REVOKED = "session.revoked"
EV_RESULT_SCHEDULED = "result.scheduled"
EV_RESULT_PUBLISHED = "result.published"
