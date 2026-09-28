"""四院正式名称：第 4 院「凤凰 / Phoenix」更正为「朱雀 / Vermilion Bird」

Revision ID: c8f3a1d7e925
Revises: b7c2e5d18a04
Create Date: 2026-09-26

为什么要改
----------
四院取名自**四象**：青龙、白虎、玄武、朱雀。当初第 4 院只有占位名
「凤凰 / Phoenix」（seed 里还留着注释"正式英文名待校方确认"），
校方现在给了正式名：**朱雀 / Vermilion Bird**。

这不是"换个好听的名字"：凤凰（Fenghuang）与朱雀（Vermilion Bird）
在中文语境里是两种不同的神兽，四象里的南方神兽是朱雀。
英文「Dragon」也一并补全为「Azure Dragon」，与另外三院
（White Tiger / Black Tortoise / Vermilion Bird）的构词保持一致。

改什么、不改什么
----------------
只改 `houses.name_cn` / `houses.name_en` 两个**展示字段**。

`houses.key`（tiger/dragon/tortoise/phoenix）**一律不动**：
它出现在外键、`match_entries.house_key_snapshot`、前端 `HOUSE_ICON` /
`INK_SHAPES` / 配色映射等大量位置，改 key 会牵动历史数据且没有任何收益 ——
key 是技术标识，不该跟着文案变。

历史成绩不受影响：比赛在报名阶段就把院名冻结进
`match_entries.house_name_snapshot`，因此**改名之前打完的比赛仍然显示当时的名字**。
这是有意的（成绩单不该被后来的改名改写），不是遗漏。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8f3a1d7e925'
down_revision: Union[str, None] = 'b7c2e5d18a04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# key → (中文名, 英文名)。key 永远不改，只改这两个给人看的字段。
RENAMES = {
    "tiger": ("白虎", "White Tiger"),
    "dragon": ("青龙", "Azure Dragon"),
    "tortoise": ("玄武", "Black Tortoise"),
    "phoenix": ("朱雀", "Vermilion Bird"),
}


def _has_houses() -> bool:
    return "houses" in sa.inspect(op.get_bind()).get_table_names()


def _apply(pairs: dict[str, tuple[str, str]]) -> None:
    if not _has_houses():
        return
    bind = op.get_bind()
    for key, (cn, en) in pairs.items():
        bind.execute(
            sa.text("UPDATE houses SET name_cn = :cn, name_en = :en WHERE key = :k"),
            {"cn": cn, "en": en, "k": key},
        )


def upgrade() -> None:
    _apply(RENAMES)


def downgrade() -> None:
    # 回滚到改名之前的文案（含占位名"凤凰 / Phoenix"）。
    # 与 upgrade 一样只碰展示字段，不动 key，也不动任何成绩快照。
    _apply({
        "tiger": ("白虎", "White Tiger"),
        "dragon": ("青龙", "Dragon"),
        "tortoise": ("玄武", "Black Tortoise"),
        "phoenix": ("凤凰", "Phoenix"),
    })
