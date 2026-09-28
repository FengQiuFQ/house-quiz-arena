"""timestamptz -> timestamp（去掉会话时区这一层解释）

Revision ID: c9a6e3b70f48
Revises: b8f5d2a96e37
Create Date: 2026-09-25

背景（PostgreSQL 演示赛实测，四院全 0 分）
------------------------------------------
时间列此前是 `timestamp with time zone`（DateTime(timezone=True)），而应用写进去的是
**naive** 时间。asyncpg 对带时区列的处理是"写：按会话时区解释 naive；读：按会话时区
返回 aware"，SQLite 则全程按文本存 naive —— 同一份代码在两种后端上语义不同。

本机现象：系统时钟是本地墙上时间而时区标成 UTC，真实 UTC 比它早 8 小时。于是库里
16:56 的回合截止时间，经会话时区读出来变成"08:56Z"，比服务端时间早 8 小时，
**所有答案提交都被判"作答通道已关闭"**；反方向（真 UTC 时钟 + 本地会话时区）会偏
另外 8 小时。两个方向都错过，说明问题在列类型，不在换算公式。

修法：两种后端同构为 `timestamp without time zone`，口径唯一 —— 入库即 UTC 墙上时间。
代码侧的 `utcnow()` 写、`as_utc()` 读全部不带 tzinfo，会话时区不再参与任何解释。

转换规则：`col::timestamp`
    naive 时间写进 timestamptz 时，PG 把**同一串数字**按会话时区存为时间点；
    转回 timestamp 时取**会话时区下的墙上时间**，拿回的就是原来那串数字。
    因此这一句是精确逆运算，且与执行迁移时的会话时区无关。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9a6e3b70f48'
down_revision: Union[str, None] = 'b8f5d2a96e37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tz_columns() -> list[tuple[str, str]]:
    """列出当前仍是 `timestamp with time zone` 的列（幂等：已转换过的会被跳过）。"""
    insp = sa.inspect(op.get_bind())
    out: list[tuple[str, str]] = []
    for table in insp.get_table_names():
        for col in insp.get_columns(table):
            if isinstance(col["type"], sa.DateTime) and col["type"].timezone:
                out.append((table, col["name"]))
    return out


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return                      # SQLite 本来就是 naive 文本，无需处理
    for table, column in _tz_columns():
        bind.exec_driver_sql(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
            f'TYPE timestamp without time zone USING "{column}"::timestamp'
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    insp = sa.inspect(bind)
    for table in insp.get_table_names():
        for col in insp.get_columns(table):
            if isinstance(col["type"], sa.DateTime) and not col["type"].timezone:
                bind.exec_driver_sql(
                    f'ALTER TABLE "{table}" ALTER COLUMN "{col["name"]}" '
                    f'TYPE timestamp with time zone USING "{col["name"]}" AT TIME ZONE \'UTC\''
                )
