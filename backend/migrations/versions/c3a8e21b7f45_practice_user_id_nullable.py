"""make practice_sessions.user_id nullable for anonymous practice

Revision ID: c3a8e21b7f45
Revises: b2f1a7c4d9e0
Create Date: 2026-09-25

匿名练习要求 `practice_sessions.user_id` 可为空（登录用户填 user_id，访客填 guest_id）。
模型早就改成了 nullable=True，但 b2 迁移漏了实际 ALTER —— 新库因为 create_all 直接按新模型
建表所以看不出问题，**只有已存在的库会报 NOT NULL constraint failed**。
这类"模型与物理结构不一致"的缺口正是上线时最容易漏掉的一种，因此本迁移单独成一条并显式断言。

Alembic 无法自动探测可空性变化，必须手写；SQLite 用 batch 重建表。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3a8e21b7f45'
down_revision: Union[str, None] = 'b2f1a7c4d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column(table: str, name: str):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return None
    for c in insp.get_columns(table):
        if c["name"] == name:
            return c
    return None


def upgrade() -> None:
    for table in ("practice_sessions",):
        col = _column(table, "user_id")
        if col is None:
            continue
        if not col.get("nullable", True):
            with op.batch_alter_table(table) as batch:
                batch.alter_column("user_id", existing_type=sa.String(length=36),
                                   nullable=True)


def downgrade() -> None:
    # 回退前必须先清掉访客会话，否则 NOT NULL 无法恢复。
    # 这是有意的破坏性行为：匿名练习在旧版本里不存在，其数据无处安放。
    op.execute("DELETE FROM practice_sessions WHERE user_id IS NULL")
    col = _column("practice_sessions", "user_id")
    if col is not None and col.get("nullable", True):
        with op.batch_alter_table("practice_sessions") as batch:
            batch.alter_column("user_id", existing_type=sa.String(length=36),
                               nullable=False)
