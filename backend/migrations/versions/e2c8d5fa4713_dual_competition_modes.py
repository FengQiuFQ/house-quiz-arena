"""双赛制：抢答（shared_buzz）与接力（relay）

Revision ID: e2c8d5fa4713
Revises: d1b7c4e95a36
Create Date: 2026-09-26

赛制定义（详见 models.py 常量区）
--------------------------------
shared_buzz  一次竞赛一个**大题库**：全场共用一份题目，四院同题抢答，控制器逐题推进。
relay        每个 House **固定题目数量**：题库按院均分，同轮各院做不同的题、并行推进，
             评分标准里纳入"做完全部题目的总用时"与"每题用时"，并支持镜像回放。

新增列
------
matches.competition_mode      赛制，默认 shared_buzz（存量比赛都是抢答，语义不变）
matches.questions_per_house   接力赛每院题量（抢答为 NULL）
matches.weight_time           接力赛时间权重 0–1000（抢答用 weight_speed）

round_instances.house_scope   本回合归属哪个学院（JSON 数组，接力赛用；抢答为 NULL）

为什么 house_scope 放在**回合**上而不是新建一张"题目分配表"：
接力赛里"某院的一道题 + 它的开放/截止时间 + 作答状态"与抢答的回合是同一种东西，
只是归属从"全场"收窄到"一个学院"。复用回合表意味着截止判定、幂等提交、
作废与备用题顶替、事件广播这些已经验证过的逻辑**一行都不用改**。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2c8d5fa4713'
down_revision: Union[str, None] = 'd1b7c4e95a36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has("matches", "competition_mode"):
        op.add_column("matches", sa.Column("competition_mode", sa.String(length=16),
                                           nullable=False, server_default="shared_buzz"))
    if not _has("matches", "questions_per_house"):
        op.add_column("matches", sa.Column("questions_per_house", sa.Integer(), nullable=True))
    if not _has("matches", "weight_time"):
        op.add_column("matches", sa.Column("weight_time", sa.Integer(),
                                           nullable=False, server_default="300"))
    if not _has("round_instances", "house_scope"):
        op.add_column("round_instances", sa.Column("house_scope", sa.JSON(), nullable=True))


def downgrade() -> None:
    for table, column in (("round_instances", "house_scope"),
                          ("matches", "weight_time"),
                          ("matches", "questions_per_house"),
                          ("matches", "competition_mode")):
        if _has(table, column):
            op.drop_column(table, column)
