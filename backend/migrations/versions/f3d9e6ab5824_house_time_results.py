"""接力赛用时成绩表 house_time_results

Revision ID: f3d9e6ab5824
Revises: e2c8d5fa4713
Create Date: 2026-09-26

接力赛的评分标准里有"做完全部题目的总用时"与"每题用时"，它们是**计分依据**
（综合评估模式下直接进入分数），必须与成绩版本一起冻结、可追溯、可被程序核对。

为什么不塞进 result_versions.notes：
notes 是给学生看的中文口径说明，混进机器数据会让结果页把 JSON 当说明印出来，
而且"分数是怎么算出来的"就没法被程序验证了。

字段单位一律毫秒（整数），与 answers.elapsed_ms 同源；
time_factor_permille 存"以最快学院为基准"的时间系数 ×1000（整数，跨库比较精确）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3d9e6ab5824'
down_revision: Union[str, None] = 'e2c8d5fa4713'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "house_time_results" in insp.get_table_names():
        return
    op.create_table(
        "house_time_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("result_version_id", sa.String(length=36), nullable=False),
        sa.Column("house_id", sa.String(length=36), nullable=False),
        sa.Column("house_key_snapshot", sa.String(length=32), nullable=False),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("per_question_ms", sa.Integer(), nullable=True),
        sa.Column("answered_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("time_factor_permille", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["result_version_id"], ["result_versions.id"]),
        sa.ForeignKeyConstraint(["house_id"], ["houses.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_house_time_results"),
        sa.UniqueConstraint("result_version_id", "house_id", name="uq_htr_ver_house"),
    )
    op.create_index("ix_house_time_results_result_version_id", "house_time_results",
                    ["result_version_id"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "house_time_results" in insp.get_table_names():
        op.drop_table("house_time_results")
