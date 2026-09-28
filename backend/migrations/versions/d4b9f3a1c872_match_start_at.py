"""add matches.start_at for homepage countdown

Revision ID: d4b9f3a1c872
Revises: c3a8e21b7f45
Create Date: 2026-09-25

首页需要在 `lobby` 阶段显示"距开赛 mm:ss"的倒计时，因此比赛需要一个**预定开赛时间**。
它与 `ceremony_start_at / reveal_at` 同性质：面向展示的时间点，不参与权威时序判定
（作答截止仍以各回合的服务端时间为准），因此允许晚到或早到。

历史数据为 NULL：首页在没有 start_at 时显示"即将开始"，而不是伪造一个倒计时。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4b9f3a1c872'
down_revision: Union[str, None] = 'c3a8e21b7f45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has_column("matches", "start_at"):
        op.add_column("matches", sa.Column("start_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column("matches", "start_at"):
        op.drop_column("matches", "start_at")
