"""match spectator_mode + per-round submission matrix

Revision ID: b8f5d2a96e37
Revises: a7e4c1f85d26
Create Date: 2026-09-25

1. `matches.spectator_mode`：观战三档 mirror / blur / summary，默认 mirror。
   安全性依据见 models.py 常量区注释（观战已限定为本院，镜像不构成额外泄露）。
2. 本迁移不含"提交矩阵"的存储：矩阵是**实时聚合**（按回合 × 学院统计已交人数），
   不落库——落库会引入一致性负担，而这是纯展示数据，随时可重算。
   历史比赛默认 mirror，与"没有观战功能"等价（前端此前不读该字段）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8f5d2a96e37'
down_revision: Union[str, None] = 'a7e4c1f85d26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has("matches", "spectator_mode"):
        op.add_column("matches", sa.Column("spectator_mode", sa.String(length=16),
                                           nullable=False, server_default="mirror"))


def downgrade() -> None:
    if _has("matches", "spectator_mode"):
        op.drop_column("matches", "spectator_mode")
