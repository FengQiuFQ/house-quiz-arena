"""取消「参赛角色」：entrant 合并进 student

Revision ID: b7c2e5d18a04
Revises: a4e1f7c2b935
Create Date: 2026-09-26

为什么去掉这个角色
------------------
原来的规则是：`student` 只能练习与上排行榜，`entrant` 才能被加进正式赛名单
（而且还要管理员再单独加一条"资格"）。三处代价都落在用户身上：

  1. 学生自助注册拿到的默认角色是 student，于是**注册完发现进不了比赛**，
     而页面上唯一的提示是"参加正式比赛需由管理员加入参赛名单"——他没有任何办法自己解决；
  2. 资格是**第二道门**：即使角色是 entrant，也要管理员再点一次"加资格"，
     现场经常出现"人到齐了但名单是空的"；
  3. 判定写成 `role in ("entrant","admin")` 之后，管理员自己也被卷进参赛语义
     （换台电脑就被当成"正在比赛"）。

现行规则是**按学院参赛**：账号绑定了学院，就能代表本学院签到上场。
角色从此只表示额外能力（admin 管后台、advisor 发起求援），不再表示能不能参赛。

本迁移做一件小事：把库里残留的 `entrant` 全部改写成 `student`。
`match_eligibilities` 表**保留不删**：它已经不再决定任何人能否参赛（签到不再查它），
但保留可以让历史数据与既有脚本继续可用，删除一张有历史记录的表不值得。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c2e5d18a04'
down_revision: Union[str, None] = 'a4e1f7c2b935'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_users_table() -> bool:
    return "users" in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_users_table():
        return
    op.execute(sa.text("UPDATE users SET role = 'student' WHERE role = 'entrant'"))


def downgrade() -> None:
    # 不可逆：合并之后无法分辨哪些 student 原本是 entrant。
    # 这里刻意**不做任何事**而不是随便挑一批改回去 —— 错误的角色比没有角色更难查。
    pass
