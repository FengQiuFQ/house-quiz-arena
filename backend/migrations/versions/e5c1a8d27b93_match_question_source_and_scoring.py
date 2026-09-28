"""match question source, scoring method, difficulty mix, start time

Revision ID: e5c1a8d27b93
Revises: d4b9f3a1c872
Create Date: 2026-09-25

后台建赛新增三项可选配置，首页与结果页据此向学生说明口径：
- `question_source`   题源：random 随机 / custom 自定义 / blueprint 主题配额
- `scoring_method`    评分标准：avg 人均平均 / total 总分 / top_n 前 N 名平均 / accuracy 正确率
- `scoring_top_n`     top_n 的 N（默认 5）
- `difficulty_mix`    难度配比 {"1":n,"2":n,"3":n}，空 = 不限

全部带 server_default，历史比赛自动落在与旧实现**完全一致**的口径上
（随机抽题 + 人均平均分），因此既有成绩快照不需要重算。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5c1a8d27b93'
down_revision: Union[str, None] = 'd4b9f3a1c872'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    have = _cols("matches")
    if "question_source" not in have:
        op.add_column("matches", sa.Column("question_source", sa.String(length=16),
                                           nullable=False, server_default="random"))
    if "scoring_method" not in have:
        op.add_column("matches", sa.Column("scoring_method", sa.String(length=16),
                                           nullable=False, server_default="avg"))
    if "scoring_top_n" not in have:
        op.add_column("matches", sa.Column("scoring_top_n", sa.Integer(),
                                           nullable=False, server_default="5"))
    if "difficulty_mix" not in have:
        op.add_column("matches", sa.Column("difficulty_mix", sa.JSON(), nullable=True))


def downgrade() -> None:
    have = _cols("matches")
    for c in ("difficulty_mix", "scoring_top_n", "scoring_method", "question_source"):
        if c in have:
            op.drop_column("matches", c)
