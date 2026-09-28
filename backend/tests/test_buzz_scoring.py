"""抢答赛制与评分模式（正确率 / 综合评估 + 权重）。

这两条都是产品硬要求，且都属于"算错了没人当场看得出来"的逻辑，
所以必须有独立回归：

抢答（buzz）
    读题窗口为 0：题目一出现就开放作答，**没有**"只能看不能答"的窗口。
    旧的"先读题后作答"仍可用（display_seconds > 0），但那必须是一个显式选择。

评分模式
    pure       只看正确率：答得慢但答得准不吃亏。
    composite  综合评估：正确率与抢答速度按权重合成；速度按**同一题最快答对者**折算。
    权重与模式一起构成**赛前冻结的计分口径**：开赛后修改会被拒绝——
    否则已经打过的回合与新规则不一致，四院之间不再可比。
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.security import new_id
from app.db.base import utcnow
from app.db.models import (
    Answer,
    House,
    Match,
    MatchEntry,
    MatchQuestion,
    ParticipantResult,
    QuestionVersion,
    ResultVersion,
    RoundInstance,
    ROUND_CLOSED,
    User,
)
from app.db.session import make_session_factory
from app.modules.competitions.rules import round_speed_factor
from app.modules.scoring.service import calculate_result, results_payload
from tests.conftest import advance_factory, command, login, make_client, make_match


# ── 纯函数层：速度系数 ─────────────────────────────────────────────────────

def test_speed_factor_rewards_the_fastest_and_never_rewards_slow():
    assert round_speed_factor(1000, 1000).display() == 1.0
    assert round_speed_factor(2000, 1000).display() == 0.5
    assert round_speed_factor(4000, 1000).display() == 0.25
    # 缺数据一律 0：宁可少给分，也不凭空给一个好看的数字
    assert round_speed_factor(None, 1000).display() == 0.0
    assert round_speed_factor(1000, None).display() == 0.0


# ── 评分维度：正确率 / 速度 / 参与度 ───────────────────────────────────────

def test_participation_rate_bounds_and_edges():
    from app.modules.competitions.rules import participation_rate

    assert participation_rate(4, 4).display() == 1.0
    assert participation_rate(2, 4).display() == 0.5
    assert participation_rate(0, 4).display() == 0.0
    # 分母为 0（没人被冻结）时不产生除零，也不凭空给满分
    assert participation_rate(0, 0).display() == 0.0
    # 超过分母的输入被夹到 1（脏数据不该造出 >100% 的参与度）
    assert participation_rate(9, 4).display() == 1.0


def test_participation_weight_defaults_to_off():
    """参与度是**显式选择**的维度：权重 0 时不得影响分数。"""
    from app.modules.competitions.rules import Fraction, composite_score

    speed = Fraction(1, 1)
    without = composite_score(4, 10, speed, 4, 700, 300)
    # 传入参与度但权重为 0：结果必须完全一致
    same = composite_score(4, 10, speed, 4, 700, 300, Fraction(0, 1), 0)
    assert without.num * same.den == same.num * without.den, "参与度权重 0 时不应改变分数"

    # 权重打开后，参与度高的一方必须严格更高（其他条件相同）
    low = composite_score(4, 10, speed, 4, 600, 200, Fraction(1, 4), 200)
    high = composite_score(4, 10, speed, 4, 600, 200, Fraction(1, 1), 200)
    assert high.num * low.den > low.num * high.den, \
        f"参与度更高应得分更高：low={low.display()} high={high.display()}"


def test_composite_stays_comparable_with_three_dimensions():
    """三维（正确率 + 速度 + 参与度）合成后仍是 0–100，且口径说明与实现一致。"""
    from app.modules.competitions.rules import Fraction, composite_score, scoring_mode_note

    # 三项全满分 → 100；三项全零 → 0
    full = composite_score(10, 10, Fraction(3, 1), 3, 500, 300, Fraction(1, 1), 200)
    assert abs(full.display() - 100.0) < 0.01, full.display()
    zero = composite_score(0, 10, Fraction(0, 1), 0, 500, 300, Fraction(0, 1), 200)
    assert zero.display() == 0.0

    note = scoring_mode_note("composite", 600, 200, "shared_buzz", 200)
    assert "正确率 60%" in note and "抢答速度 20%" in note and "参与度 20%" in note, note
    # 参与度未启用时不得在手口径里出现（避免给学生一条权重 0 的噪音说明）
    assert "参与度" not in scoring_mode_note("composite", 700, 300, "shared_buzz", 0)


# ── 分数必须能落库 ─────────────────────────────────────────────────────────

def test_composite_scores_stay_inside_int32():
    """综合评分的结果必须能存进 INTEGER 列（PostgreSQL int32）。

    踩过的坑：正确率与速度的分母相乘会让分子膨胀到 3.87×10¹³。
    SQLite 是动态类型会默默存下，PostgreSQL 直接
    `invalid input for query argument ... value out of int32 range` 500 ——
    只有真跑 PG 才会暴露（本项目在演示赛结算那一步撞到）。

    修法是**把对外精度变成存储精度**：分数本来就按两位小数展示，
    因此量化到 1/10000 网格既不损失任何可见信息，又把数值钉在 int32 里。
    这里把"分数必须小且落在网格上"当成硬约束钉住。
    """
    from app.modules.competitions.rules import Fraction, composite_score

    worst = 0
    off_grid = []
    for w_acc, w_speed in ((1, 999), (999, 1), (500, 500), (700, 300)):
        for answered in (1, 2, 3, 7, 20):
            for correct in range(answered + 1):
                # 速度系数用最"难约分"的形状：多个互质分母相加
                speed = Fraction(0, 1)
                for k in (3, 7, 11, 13, 17, 19):
                    speed = Fraction(speed.num * k + 1, speed.den * k)
                # 参与度也取难约分的形状（分母 7），逼出三维合成的最坏情况
                for w_part, part in ((0, None), (200, Fraction(3, 7)), (500, Fraction(5, 7))):
                    frac = composite_score(correct, 20, speed, answered, w_acc, w_speed,
                                           part, w_part)
                    worst = max(worst, abs(frac.num), frac.den)
                    if 10_000 % frac.den != 0:
                        off_grid.append(f"{frac.num}/{frac.den}")
    assert worst < 2**31 - 1, f"分数分量过大（{worst}），会超出 PostgreSQL int32"
    assert worst < 10**6, f"即使没溢出，也应该量化到小数值：{worst}"
    assert not off_grid, f"分数应落在 1/10000 网格上（否则比较与存储都不稳定）：{off_grid[:3]}"


# ── 抢答：回合一出现就能答 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buzz_round_is_open_immediately(db_setup, client):
    """读题 0 秒：回合创建出来就是 open，open_at == 创建时刻，且能立刻提交。"""
    match_id = await make_match(db_setup, client, question_count=1, display=0, answer=60)
    r = await command(client, match_id, "start_match")
    assert r.status_code == 200, r.text
    await advance_factory()(match_id, utcnow())

    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        rd = await db.get(RoundInstance, m.current_round_id)
        assert rd is not None and rd.status == "open", f"抢答回合应直接开放，实际 {rd and rd.status}"
        assert rd.open_at - rd.display_at < timedelta(seconds=1), "抢答回合不应有读题窗口"
        assert rd.deadline_at > rd.open_at

    pc = make_client()
    try:
        await login(pc, "p_tiger_1", "pass-123456")
        snap = (await pc.get(f"/api/v1/matches/{match_id}/snapshot")).json()
        rd = snap["round"]
        assert snap["match"]["buzz"] is True, "快照必须告诉前端这是抢答场"
        assert rd["phase"] == "open"
        claim = await pc.post("/api/v1/auth/control/claim")
        ctl = claim.json()["control_token"]
        sub = await pc.post(
            f"/api/v1/matches/{match_id}/rounds/{rd['round_instance_id']}/answers",
            json={"option_id": "o1"},
            headers={"Idempotency-Key": f"buzz-{new_id()[:16]}", "X-Control-Token": ctl})
        assert sub.status_code == 200, f"抢答回合应立即受理提交：{sub.text}"
    finally:
        await pc.aclose()


@pytest.mark.asyncio
async def test_reading_window_is_still_available_when_explicitly_set(db_setup, client):
    """显式设了读题秒数时，旧行为保留：先 displaying，到点才 open。"""
    match_id = await make_match(db_setup, client, question_count=1, display=2, answer=30)
    r = await command(client, match_id, "start_match")
    assert r.status_code == 200, r.text
    await advance_factory()(match_id, utcnow())

    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        rd = await db.get(RoundInstance, m.current_round_id)
        assert rd.status == "displaying"
        assert rd.open_at - rd.display_at >= timedelta(seconds=1)


# ── 评分模式：只看正确率 vs 综合评估 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_composite_scores_faster_correct_answer_higher(db_setup, client):
    """综合评估：同样答对，抢答更快的人分更高；只看正确率时两人完全相同。"""
    # 只建一场（题目数 1），两名选手同题；用时差异直接写进 Answer.elapsed_ms
    async def build(mode: str, weight_speed: int):
        match_id = await make_match(db_setup, client, question_count=1, display=0, answer=60)
        r = await command(client, match_id, "start_match")
        assert r.status_code == 200, r.text
        await advance_factory()(match_id, utcnow())
        factory = make_session_factory()
        async with factory() as db:
            m = await db.get(Match, match_id)
            m.scoring_mode = mode
            m.weight_accuracy = 1000 - weight_speed
            m.weight_speed = weight_speed
            rd = await db.get(RoundInstance, m.current_round_id)
            rd.status = ROUND_CLOSED
            rd.closed_at = utcnow()
            m.current_round_id = None
            mq = (await db.execute(
                select(MatchQuestion).where(
                    MatchQuestion.match_id == match_id, MatchQuestion.is_backup.is_(False))
            )).scalars().first()
            v = await db.get(QuestionVersion, mq.version_id)
            entries = (await db.execute(
                select(MatchEntry).where(MatchEntry.match_id == match_id)
            )).scalars().all()
            fast, slow = entries[0], entries[1]
            # 注意：Answer.id 是自增 Integer（不是 String36），不要显式传 id
            db.add_all([
                Answer(match_id=match_id, round_instance_id=rd.id, entry_id=fast.id,
                       option_id=v.correct_option_id, idempotency_key=f"k{new_id()[:16]}",
                       received_at=utcnow(), receipt_id=f"r{new_id()[:8]}", fingerprint="fp1",
                       elapsed_ms=1200),
                Answer(match_id=match_id, round_instance_id=rd.id, entry_id=slow.id,
                       option_id=v.correct_option_id, idempotency_key=f"k{new_id()[:16]}",
                       received_at=utcnow(), receipt_id=f"r{new_id()[:8]}", fingerprint="fp2",
                       elapsed_ms=9600),
            ])
            await db.commit()
            rv = await calculate_result(db, m, None)
            await db.commit()
            payload = await results_payload(db, m, rv)
            rows = (await db.execute(
                select(ParticipantResult).where(
                    ParticipantResult.result_version_id == rv.id)
            )).scalars().all()
            by_entry = {p.entry_id: p for p in rows}
            return (payload, by_entry[fast.id], by_entry[slow.id])

    # 只看正确率：两人都答对 1/1 → 同分
    _, pure_fast, pure_slow = await build("pure", 0)
    assert pure_fast.score_num * pure_slow.score_den == pure_slow.score_num * pure_fast.score_den, \
        "只看正确率时，快慢不应影响分数"

    # 综合评估（速度权重 300）：快的人严格更高
    payload, comp_fast, comp_slow = await build("composite", 300)
    assert comp_fast.score_num * comp_slow.score_den > comp_slow.score_num * comp_fast.score_den, \
        f"综合评估应奖励更快的正确作答：fast={comp_fast.score_num}/{comp_fast.score_den} " \
        f"slow={comp_slow.score_num}/{comp_slow.score_den}"
    # 口径必须随结果一起给出（学生要能追溯每一分）
    assert payload["match"]["scoring_mode"] == "composite"
    assert payload["match"]["weight_speed"] == 300
    assert "速度" in payload["match"]["scoring_note"]


@pytest.mark.asyncio
async def test_weight_zero_speed_ignores_speed_entirely(db_setup, client):
    """速度权重为 0 时，综合评估退化为纯正确率：快慢不再影响结果。"""
    match_id = await make_match(db_setup, client, question_count=1, display=0, answer=60)
    r = await command(client, match_id, "start_match")
    assert r.status_code == 200, r.text
    await advance_factory()(match_id, utcnow())
    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        m.scoring_mode = "composite"
        m.weight_accuracy = 1000
        m.weight_speed = 0
        rd = await db.get(RoundInstance, m.current_round_id)
        rd.status = ROUND_CLOSED
        rd.closed_at = utcnow()
        m.current_round_id = None
        mq = (await db.execute(select(MatchQuestion).where(
            MatchQuestion.match_id == match_id, MatchQuestion.is_backup.is_(False)))).scalars().first()
        v = await db.get(QuestionVersion, mq.version_id)
        entries = (await db.execute(select(MatchEntry).where(
            MatchEntry.match_id == match_id))).scalars().all()
        a, b = entries[0], entries[1]
        for e, ms in ((a, 500), (b, 15000)):
            db.add(Answer(match_id=match_id, round_instance_id=rd.id, entry_id=e.id,
                          option_id=v.correct_option_id, idempotency_key=f"k{new_id()[:16]}",
                          received_at=utcnow(), receipt_id=f"r{new_id()[:8]}",
                          fingerprint=f"fp{ms}", elapsed_ms=ms))
        await db.commit()
        rv = await calculate_result(db, m, None)
        await db.commit()
        rows = (await db.execute(select(ParticipantResult).where(
            ParticipantResult.result_version_id == rv.id))).scalars().all()
        by = {p.entry_id: p for p in rows}
        assert by[a.id].score_num * by[b.id].score_den == by[b.id].score_num * by[a.id].score_den, \
            "速度为 0 权重时快慢不应影响分数"


# ── 赛前冻结：开赛后不得改口径 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scoring_config_editable_before_start_and_frozen_after(db_setup, client):
    match_id = await make_match(db_setup, client, question_count=1, display=0, answer=30)
    # draft 阶段可以改
    r = await client.patch(f"/api/v1/admin/matches/{match_id}",
                           json={"scoring_mode": "composite", "weight_accuracy": 600, "weight_speed": 400})
    assert r.status_code == 200, r.text
    assert r.json()["changed"]["scoring_mode"] == "composite"

    # 权重全 0 必须被拒（否则综合评估会静默退化成 0 分）
    bad = await client.patch(f"/api/v1/admin/matches/{match_id}",
                             json={"weight_accuracy": 0, "weight_speed": 0})
    assert bad.status_code == 422, bad.text

    rr = await command(client, match_id, "start_match")
    assert rr.status_code == 200, rr.text
    after = await client.patch(f"/api/v1/admin/matches/{match_id}", json={"scoring_mode": "pure"})
    assert after.status_code == 409, f"开赛后改计分口径必须被拒：{after.text}"
    assert "开赛前" in after.json()["message"]


@pytest.mark.asyncio
async def test_public_board_is_anonymous_and_has_no_question_content(db_setup, client):
    """/board：匿名可读（大屏不需要登录），且**不含题面**。"""
    match_id = await make_match(db_setup, client, question_count=1, display=0, answer=60)
    r = await command(client, match_id, "start_match")
    assert r.status_code == 200, r.text
    await advance_factory()(match_id, utcnow())

    anon = make_client()
    try:
        b = await anon.get(f"/api/v1/matches/{match_id}/board")
        assert b.status_code == 200, f"大屏必须匿名可读：{b.status_code} {b.text}"
        body = b.json()
        assert body["anonymous"] is True
        assert body["round"]["prompt_included"] is False
        assert "prompt" not in body["round"] and "options" not in body["round"]
        assert body["progress"]["houses"], "大屏要能看到四院进度"
        # 匿名依然拿不到答题快照（那是"我自己要答题"的完整视图）
        s = await anon.get(f"/api/v1/matches/{match_id}/snapshot")
        assert s.status_code == 401
    finally:
        await anon.aclose()
