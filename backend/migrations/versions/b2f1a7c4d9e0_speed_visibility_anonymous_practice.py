"""b2 speed timing, live visibility, anonymous practice and leaderboard

Revision ID: b2f1a7c4d9e0
Revises: adc6e0161fa4
Create Date: 2026-09-25

全部为**加列 + 加表**，不删不改既有列：
- `answers.elapsed_ms` / `practice_answers.elapsed_ms`：服务端计算的作答用时（速度榜与题目难度分析的基础）。
  历史数据为 NULL，展示层按「无样本」处理，**绝不伪记为 0 秒**。
- `matches.live_visibility`：赛中可见性三档（off / house / individual），默认 house。
- 匿名练习：`guest_identities`（令牌只存散列）+ `practice_sessions` 的访客与结算列。

**幂等设计**：SQLite 的 DDL 不受事务保护，一次失败的升级会留下「部分已应用」的库。
生产上这比升级本身更常见，因此本迁移的每一步都先探测当前结构再执行，
使其在「全新库」与「半应用库」上都能安全跑到 head。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2f1a7c4d9e0'
down_revision: Union[str, None] = 'adc6e0161fa4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── 结构探测（幂等的前提）────────────────────────────────────────────────────

def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return any(c["name"] == column for c in _inspector().get_columns(table))


def _has_index(table: str, index: str) -> bool:
    if not _has_table(table):
        return False
    return any(i["name"] == index for i in _inspector().get_indexes(table))


def _add_column(table: str, column: sa.Column) -> None:
    if not _has_column(table, column.name):
        op.add_column(table, column)


def _create_index(name: str, table: str, cols: list[str]) -> None:
    if not _has_index(table, name):
        op.create_index(name, table, cols)


def upgrade() -> None:
    # ── ① 作答用时持久化 ────────────────────────────────────────────────────
    _add_column('answers', sa.Column('elapsed_ms', sa.Integer(), nullable=True))
    _create_index('ix_answers_speed', 'answers', ['match_id', 'elapsed_ms'])
    _add_column('practice_answers', sa.Column('elapsed_ms', sa.Integer(), nullable=True))

    # ── ② 赛中可见性开关（默认 house，兼顾参与感与悬念）─────────────────────
    _add_column('matches', sa.Column('live_visibility', sa.String(length=16),
                                     nullable=False, server_default='house'))

    # ── ③ 匿名身份（令牌只存散列，序号稳定用于显示「枫禾学子 #nn」）─────────
    if not _has_table('guest_identities'):
        op.create_table(
            'guest_identities',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('display_seq', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint('id', name=op.f('pk_guest_identities')),
            sa.UniqueConstraint('token_hash', name=op.f('uq_guest_identities_token_hash')),
            sa.UniqueConstraint('display_seq', name=op.f('uq_guest_identities_display_seq')),
        )
    _create_index(op.f('ix_guest_identities_display_seq'), 'guest_identities', ['display_seq'])

    # ── ④ 练习会话：访客通道 + 结算与上榜字段 ───────────────────────────────
    # SQLite 下加外键必须 batch 重建表（render_as_batch 已在 env.py 开启）。
    # batch 重建会整表复制，因此只在**确实缺列**时才做，避免无谓的全表重写。
    ps_cols = {c["name"] for c in _inspector().get_columns('practice_sessions')}
    new_cols = [c for c in ('guest_id', 'answered_count', 'correct_count', 'score_num',
                            'score_den', 'total_ms', 'best_streak', 'is_ranked',
                            'display_name', 'house_id') if c not in ps_cols]
    if new_cols:
        with op.batch_alter_table('practice_sessions') as batch:
            if 'guest_id' not in ps_cols:
                batch.add_column(sa.Column('guest_id', sa.String(length=36), nullable=True))
            if 'answered_count' not in ps_cols:
                batch.add_column(sa.Column('answered_count', sa.Integer(), nullable=False,
                                           server_default='0'))
            if 'correct_count' not in ps_cols:
                batch.add_column(sa.Column('correct_count', sa.Integer(), nullable=False,
                                           server_default='0'))
            if 'score_num' not in ps_cols:
                batch.add_column(sa.Column('score_num', sa.Integer(), nullable=False,
                                           server_default='0'))
            if 'score_den' not in ps_cols:
                batch.add_column(sa.Column('score_den', sa.Integer(), nullable=False,
                                           server_default='1'))
            if 'total_ms' not in ps_cols:
                batch.add_column(sa.Column('total_ms', sa.Integer(), nullable=True))
            if 'best_streak' not in ps_cols:
                batch.add_column(sa.Column('best_streak', sa.Integer(), nullable=False,
                                           server_default='0'))
            if 'is_ranked' not in ps_cols:
                batch.add_column(sa.Column('is_ranked', sa.Boolean(), nullable=False,
                                           server_default=sa.false()))
            if 'display_name' not in ps_cols:
                batch.add_column(sa.Column('display_name', sa.String(length=32), nullable=True))
            if 'house_id' not in ps_cols:
                batch.add_column(sa.Column('house_id', sa.String(length=36), nullable=True))
            if not _has_index('practice_sessions', 'ix_practice_sessions_guest_id'):
                batch.create_index('ix_practice_sessions_guest_id', ['guest_id'])
            if not _has_index('practice_sessions', 'ix_practice_sessions_is_ranked'):
                batch.create_index('ix_practice_sessions_is_ranked', ['is_ranked'])
            fks = {fk["name"] for fk in _inspector().get_foreign_keys('practice_sessions')}
            if 'fk_practice_sessions_guest_id' not in fks:
                batch.create_foreign_key('fk_practice_sessions_guest_id', 'guest_identities',
                                         ['guest_id'], ['id'])
            if 'fk_practice_sessions_house_id' not in fks:
                batch.create_foreign_key('fk_practice_sessions_house_id', 'houses',
                                         ['house_id'], ['id'])


def downgrade() -> None:
    """回退。同样逐项探测：只删确实存在的东西。"""
    if _has_table('practice_sessions'):
        ps_cols = {c["name"] for c in _inspector().get_columns('practice_sessions')}
        fks = {fk["name"] for fk in _inspector().get_foreign_keys('practice_sessions')}
        with op.batch_alter_table('practice_sessions') as batch:
            if 'fk_practice_sessions_house_id' in fks:
                batch.drop_constraint('fk_practice_sessions_house_id', type_='foreignkey')
            if 'fk_practice_sessions_guest_id' in fks:
                batch.drop_constraint('fk_practice_sessions_guest_id', type_='foreignkey')
            if _has_index('practice_sessions', 'ix_practice_sessions_is_ranked'):
                batch.drop_index('ix_practice_sessions_is_ranked')
            if _has_index('practice_sessions', 'ix_practice_sessions_guest_id'):
                batch.drop_index('ix_practice_sessions_guest_id')
            for col in ('house_id', 'display_name', 'is_ranked', 'best_streak', 'total_ms',
                        'score_den', 'score_num', 'correct_count', 'answered_count', 'guest_id'):
                if col in ps_cols:
                    batch.drop_column(col)
    if _has_table('guest_identities'):
        if _has_index('guest_identities', op.f('ix_guest_identities_display_seq')):
            op.drop_index(op.f('ix_guest_identities_display_seq'), table_name='guest_identities')
        op.drop_table('guest_identities')
    if _has_column('matches', 'live_visibility'):
        op.drop_column('matches', 'live_visibility')
    if _has_column('practice_answers', 'elapsed_ms'):
        op.drop_column('practice_answers', 'elapsed_ms')
    if _has_index('answers', 'ix_answers_speed'):
        op.drop_index('ix_answers_speed', table_name='answers')
    if _has_column('answers', 'elapsed_ms'):
        op.drop_column('answers', 'elapsed_ms')
