"""比赛状态机（§4、§15.1）：合法转换表 + 回合生命周期。

比赛状态与回合状态严格分离。任何非法转换在服务端被拒（INVALID_TRANSITION），
不能只靠前端禁用按钮。
"""

# 比赛/回合状态与模式常量从 models 引入，避免第二处定义
from app.db.models import (  # noqa: F401
    MATCH_ARCHIVED, MATCH_CALCULATED, MATCH_CANCELLED, MATCH_DRAFT, MATCH_LOCKED,
    MATCH_LOBBY, MATCH_PAUSED, MATCH_REVEALED, MATCH_REVEAL_SCHEDULED, MATCH_RUNNING,
    MODE_FIXED_K, MODE_OPEN,
    ROUND_CLOSED, ROUND_DISPLAYING, ROUND_OPEN, ROUND_PENDING, ROUND_VOID,
)

LEGAL_TRANSITIONS: dict[str, set[str]] = {
    MATCH_DRAFT: {MATCH_LOBBY, MATCH_CANCELLED},
    MATCH_LOBBY: {MATCH_RUNNING, MATCH_DRAFT, MATCH_CANCELLED},
    MATCH_RUNNING: {MATCH_PAUSED, MATCH_LOCKED, MATCH_CANCELLED},
    MATCH_PAUSED: {MATCH_RUNNING, MATCH_LOCKED, MATCH_CANCELLED},
    MATCH_LOCKED: {MATCH_CALCULATED},
    MATCH_CALCULATED: {MATCH_REVEAL_SCHEDULED},
    MATCH_REVEAL_SCHEDULED: {MATCH_REVEALED},
    MATCH_REVEALED: {MATCH_ARCHIVED},
    MATCH_ARCHIVED: set(),
    MATCH_CANCELLED: set(),
}

# 命令动作 → 目标状态（§15.1 命令表）
ACTION_TARGET = {
    "open_lobby": MATCH_LOBBY,
    "start_match": MATCH_RUNNING,
    "pause": MATCH_PAUSED,
    "resume": MATCH_RUNNING,
    "lock": MATCH_LOCKED,
    "calculate": MATCH_CALCULATED,
    "schedule_reveal": MATCH_REVEAL_SCHEDULED,
    "cancel": MATCH_CANCELLED,
}
# 特殊命令：不改状态（赛后更正路径，§10.3）
SPECIAL_ACTIONS = {"recalculate", "publish_correction"}

# 回合合法转换
ROUND_TRANSITIONS: dict[str, set[str]] = {
    ROUND_PENDING: {ROUND_DISPLAYING, ROUND_VOID},
    ROUND_DISPLAYING: {ROUND_OPEN, ROUND_VOID},
    ROUND_OPEN: {ROUND_CLOSED, ROUND_VOID},
    ROUND_CLOSED: {ROUND_VOID},
    ROUND_VOID: set(),
}


def can_transition(current: str, target: str, table: dict[str, set[str]] | None = None) -> bool:
    return target in table.get(current, set()) if table else target in LEGAL_TRANSITIONS.get(current, set())
