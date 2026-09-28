"""抢答赛制：读题窗口归零 + 评分模式与权重

Revision ID: d1b7c4e95a36
Revises: c9a6e3b70f48
Create Date: 2026-09-26

两件事：

1. **读题窗口归零（抢答）**
   `matches.display_seconds` 默认从 10 改为 0；已存在的比赛也一并归零。
   为什么连存量一起改：本系统的正式赛制就是抢答——题目一出现即可作答，
   读题与作答是同一段时间。留着旧值会让"改完没生效"，而且默认值本该只有一个口径。
   需要"主持人念题"的场次可以在建赛时把读题秒数设回 >0，那是显式选择。

2. **评分模式与权重**
   `scoring_mode`（pure 只看正确率 / composite 综合评估）、
   `weight_accuracy`、`weight_speed`（0–1000 的整数权重）。
   权重是**赛前冻结**的计分口径，与 `scoring_method` 同级：
   开赛后修改会让已打过的回合与新规则不一致，学院之间不再可比（服务端拒绝）。
   默认 700/300：正确率仍是主体，速度作为加分项——这样"稳且准"的队伍不会被
   单纯的抢答速度压倒，同时抢答确实值钱。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1b7c4e95a36'
down_revision: Union[str, None] = 'c9a6e3b70f48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has("matches", "scoring_mode"):
        op.add_column("matches", sa.Column("scoring_mode", sa.String(length=16),
                                           nullable=False, server_default="pure"))
    if not _has("matches", "weight_accuracy"):
        op.add_column("matches", sa.Column("weight_accuracy", sa.Integer(),
                                           nullable=False, server_default="700"))
    if not _has("matches", "weight_speed"):
        op.add_column("matches", sa.Column("weight_speed", sa.Integer(),
                                           nullable=False, server_default="300"))
    # 抢答：把存量比赛的读题窗口也归零（新默认值同时在建赛路径上生效）
    op.execute("UPDATE matches SET display_seconds = 0 WHERE display_seconds > 0")


def downgrade() -> None:
    # 无法恢复各自原来的读题秒数（归零后信息已丢失），因此只回滚列，不动 display_seconds。
    for col in ("weight_speed", "weight_accuracy", "scoring_mode"):
        if _has("matches", col):
            op.drop_column("matches", col)
