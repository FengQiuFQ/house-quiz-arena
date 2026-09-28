"""match houses_scope: restrict which houses may join/spectate

Revision ID: a7e4c1f85d26
Revises: f6d2b9e34a17
Create Date: 2026-09-25

允许"限定学院的比赛"（如两院对抗）。NULL / 空数组 = 四院全参（既有行为不变）。

服务端据此强制两条规则：
  · 选手只能加入包含自己学院的比赛（competitions.service.check_in）
  · 未揭晓前，不属于本场的学院观战接口直接拒绝（competitions.router.spectate）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7e4c1f85d26'
down_revision: Union[str, None] = 'f6d2b9e34a17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has("matches", "houses_scope"):
        op.add_column("matches", sa.Column("houses_scope", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has("matches", "houses_scope"):
        op.drop_column("matches", "houses_scope")
