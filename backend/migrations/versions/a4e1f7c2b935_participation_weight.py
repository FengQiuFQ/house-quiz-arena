"""综合评估新增「参与度」维度

Revision ID: a4e1f7c2b935
Revises: f3d9e6ab5824
Create Date: 2026-09-26

为什么要有这一维
----------------
产品要求综合评估可包含"参与度"。原来的两维（正确率 + 速度）在口径上有个盲点：
**"人均平均"下一个人都不交的学院并不吃亏**（分母只算上场的人），
而学校的组织意图常常是"让尽量多的人真的上场"。
因此把参与度做成**可加权的第三维**：
    参与度 = 本院实际提交过答案的人数 ÷ 本院冻结名单人数
它与正确率同量纲（都是比例），可以直接加权合成。

默认权重 0 = 不计入：这是**显式选择**的维度，不该由系统替学校默认开启
（否则会把"少而精"的学院悄悄压下去）。因此本迁移只加列，不改任何存量比赛的行为。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4e1f7c2b935'
down_revision: Union[str, None] = 'f3d9e6ab5824'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "matches" not in insp.get_table_names():
        return
    if any(c["name"] == "weight_participation" for c in insp.get_columns("matches")):
        return
    op.add_column("matches", sa.Column("weight_participation", sa.Integer(),
                                       nullable=False, server_default="0"))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "matches" in insp.get_table_names() \
            and any(c["name"] == "weight_participation" for c in insp.get_columns("matches")):
        op.drop_column("matches", "weight_participation")
