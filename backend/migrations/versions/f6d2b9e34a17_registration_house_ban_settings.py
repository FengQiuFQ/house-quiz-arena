"""self-registration, house lock, ban fields, runtime settings

Revision ID: f6d2b9e34a17
Revises: e5c1a8d27b93
Create Date: 2026-09-25

四组变化：
1. `users` 新增自助注册与 House 归属标记：`self_registered` / `house_locked`。
   House 归属规则：学生注册时自选，**注册后自己不能改**；只有管理员能调整。
2. `users` 新增封禁信息：`disabled_reason` / `disabled_at`（`status` 列早已存在）。
3. `users.last_login_at`：后台"最近活跃"用。
4. `runtime_settings` 表：后台运行期开关（是否允许注册、邀请码等），与部署配置分离。

全部为加列 + 加表；历史用户默认 `self_registered=false, house_locked=false`，
语义为"由管理员预建、House 可调整"，与既有行为一致。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6d2b9e34a17'
down_revision: Union[str, None] = 'e5c1a8d27b93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    have = _cols("users")
    if "self_registered" not in have:
        op.add_column("users", sa.Column("self_registered", sa.Boolean(),
                                         nullable=False, server_default=sa.false()))
    if "house_locked" not in have:
        op.add_column("users", sa.Column("house_locked", sa.Boolean(),
                                         nullable=False, server_default=sa.false()))
    if "disabled_reason" not in have:
        op.add_column("users", sa.Column("disabled_reason", sa.String(length=255), nullable=True))
    if "disabled_at" not in have:
        op.add_column("users", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
    if "last_login_at" not in have:
        op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    if "ix_users_self_registered" not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("users")}:
        op.create_index("ix_users_self_registered", "users", ["self_registered"])

    if "runtime_settings" not in _tables():
        op.create_table(
            "runtime_settings",
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("value", sa.String(length=255), nullable=False),
            sa.Column("updated_by", sa.String(length=36), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("key", name=op.f("pk_runtime_settings")),
        )


def downgrade() -> None:
    if "runtime_settings" in _tables():
        op.drop_table("runtime_settings")
    have = _cols("users")
    idx = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("users")}
    if "ix_users_self_registered" in idx:
        op.drop_index("ix_users_self_registered", table_name="users")
    for c in ("last_login_at", "disabled_at", "disabled_reason", "house_locked", "self_registered"):
        if c in have:
            op.drop_column("users", c)
