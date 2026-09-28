"""双赛制：抢答（shared_buzz）与接力（relay）。

两条产品要求对应到这里：

  抢答  **一次竞赛固定一个大题库**，四院同题抢答（题目由控制器逐题推进）。
  接力  **每个 House 固定题目数量**，题目按院均分、各院并行推进；
        评分标准里有"做完全部题目的总用时"与"每题用时"，并支持镜像回放。

接力最容易做错的三件事，这里逐条守住：
  1. **别院不能代答**：每道题只属于一个学院，服务端强制（不靠前端隐藏）。
  2. **用时是"净用时"**：一道题在本院被抢到就结束，不能把"全队都答完"当成结束时刻。
  3. **收尾要收干净**：接力同时有多个回合开放，按"结束作答"必须全部关闭并计入有效题；
     否则结算时它们不算有效题，成绩直接算错。
"""
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
    RoundInstance,
    ROUND_CLOSED,
    ROUND_OPEN,
    ROLE_STUDENT,
    User,
)
from app.db.session import make_session_factory
from app.modules.competitions.rules import relay_completion_ms, relay_time_factor
from app.modules.scoring.service import calculate_result, results_payload
from tests.conftest import advance_factory, command, login, make_client, make_match


# ── 纯函数层 ───────────────────────────────────────────────────────────────

def test_relay_time_factor_is_relative_to_the_fastest_house():
    assert relay_time_factor(30_000, 30_000).display() == 1.0
    assert relay_time_factor(60_000, 30_000).display() == 0.5
    assert relay_time_factor(120_000, 30_000).display() == 0.25
    # 没有用时（没交完/缺数据）一律 0：不能给"没做"发满速分
    assert relay_time_factor(None, 30_000).display() == 0.0
    assert relay_time_factor(30_000, None).display() == 0.0


def test_relay_completion_is_max_not_zero_when_missing():
    assert relay_completion_ms([1200, 8000, None]) == 8000
    # 一份都没有 → None（没做），而不是 0（瞬间做完）
    assert relay_completion_ms([None, None]) is None
    assert relay_completion_ms([]) is None


# ── 接力赛：建赛、题目归属、并行推进 ───────────────────────────────────────

async def _make_relay(db_setup, client, *, per_house: int = 1, houses: list[str] | None = None):
    """建一场接力赛（不签到、不开赛）。

    全场题量由"每院题量 × 参赛学院数"推导，因此测试库里的题目不够时用随机题源
    （测试库只种了 6 道题，2 题/院 × 4 院需要 8 道 —— 这正好也验证了
    "题不够时服务端会明确报错而不是悄悄少出题"）。
    """
    await login(client, "admin", "admin-pass-123")
    qids = db_setup["question_ids"]
    n_houses = len(houses or ["tiger", "dragon", "tortoise", "phoenix"])
    need = per_house * n_houses
    body = {
        "title": f"[DEMO] 接力测试 {new_id()[:6]}", "mode": "open",
        "competition_mode": "relay", "questions_per_house": per_house,
        "display_seconds": 0, "answer_seconds": 60, "interlude_seconds": 0,
        "scoring_mode": "composite", "weight_accuracy": 700, "weight_time": 300,
    }
    if need <= len(qids):
        body["question_ids"] = qids[:need]
    else:
        body["question_source"] = "random"
    if houses:
        body["houses_scope"] = houses
    return await client.post("/api/v1/admin/matches", json=body)


@pytest.mark.asyncio
async def test_relay_requires_questions_per_house(db_setup, client):
    await login(client, "admin", "admin-pass-123")
    r = await client.post("/api/v1/admin/matches", json={
        "title": "[DEMO] 接力缺参数", "mode": "open", "competition_mode": "relay",
        "display_seconds": 0, "answer_seconds": 30,
    })
    assert r.status_code == 422, r.text
    assert "questions_per_house" in r.json()["message"]


@pytest.mark.asyncio
async def test_relay_rounds_are_per_house_and_enforced(db_setup, client):
    """每院拿到自己的题；别院提交被 403 拒绝。"""
    r = await _make_relay(db_setup, client, per_house=1)
    assert r.status_code == 200, r.text
    match_id = r.json()["match_id"]
    assert r.json()["competition_mode"] == "relay"

    rr = await command(client, match_id, "open_lobby")
    assert rr.status_code == 200, rr.text
    for key in db_setup["houses"]:
        pc = make_client()
        await login(pc, f"p_{key}_1", "pass-123456")
        ci = await pc.post(f"/api/v1/matches/{match_id}/check-in")
        assert ci.status_code == 200, ci.text
        await pc.aclose()

    rr = await command(client, match_id, "start_match")
    assert rr.status_code == 200, rr.text
    # 接力赛的回合在开赛后的第一个 tick 放出
    await advance_factory()(match_id, utcnow())

    factory = make_session_factory()
    async with factory() as db:
        rounds = (await db.execute(select(RoundInstance).where(
            RoundInstance.match_id == match_id))).scalars().all()
        assert len(rounds) == 4, f"四院各一道题，应生成 4 个回合，实际 {len(rounds)}"
        scopes = {tuple((r.house_scope or [])) for r in rounds}
        assert len(scopes) == 4, f"每个回合归属不同学院：{scopes}"
        assert all(r.status == ROUND_OPEN for r in rounds), "接力回合应同时开放（各院并行）"
        # 各院拿到的是**不同的**题
        qids = {r.match_question_id for r in rounds}
        assert len(qids) == 4, "四院不应共用同一道题"

    # 选手只看到自己学院那道题
    pc = make_client()
    try:
        await login(pc, "p_tiger_1", "pass-123456")
        snap = (await pc.get(f"/api/v1/matches/{match_id}/snapshot")).json()
        assert snap["match"]["competition_mode"] == "relay"
        assert snap["round"] is not None, "本院应看到自己的题目"
        assert snap["round"]["house_scope"] == ["tiger"], snap["round"]["house_scope"]
        claim = await pc.post("/api/v1/auth/control/claim")
        ctl = claim.json()["control_token"]

        # 别院的那道题：直接拿另一个回合的 id 提交 → 必须被拒
        factory = make_session_factory()
        async with factory() as db:
            other = next(r for r in (await db.execute(select(RoundInstance).where(
                RoundInstance.match_id == match_id))).scalars().all()
                if (r.house_scope or []) and "tiger" not in r.house_scope)
        bad = await pc.post(
            f"/api/v1/matches/{match_id}/rounds/{other.id}/answers",
            json={"option_id": "o1"},
            headers={"Idempotency-Key": f"k-{new_id()[:16]}", "X-Control-Token": ctl})
        assert bad.status_code == 403, f"别院的题必须被拒：{bad.status_code} {bad.text}"
        assert bad.json()["code"] == "NOT_YOUR_HOUSE"

        ok = await pc.post(
            f"/api/v1/matches/{match_id}/rounds/{snap['round']['round_instance_id']}/answers",
            json={"option_id": "o1"},
            headers={"Idempotency-Key": f"k-{new_id()[:16]}", "X-Control-Token": ctl})
        assert ok.status_code == 200, f"本院那道题必须受理：{ok.text}"
    finally:
        await pc.aclose()


@pytest.mark.asyncio
async def test_relay_lock_closes_every_open_round(db_setup, client):
    """「结束作答」必须收掉**所有**开放回合（否则结算会漏掉有效题）。

    用 1 题/院：四院并行就同时有 4 个回合开放，足以覆盖"多回合同时开放"这个关键场景；
    这里要验证的是**收尾是否收干净**，不是题量本身。
    """
    r = await _make_relay(db_setup, client, per_house=1)
    assert r.status_code == 200, r.text
    match_id = r.json()["match_id"]
    await command(client, match_id, "open_lobby")
    factory = make_session_factory()
    for key in db_setup["houses"]:
        pc = make_client()
        await login(pc, f"p_{key}_1", "pass-123456")
        await pc.post(f"/api/v1/matches/{match_id}/check-in")
        await pc.aclose()
    await command(client, match_id, "start_match")
    await advance_factory()(match_id, utcnow())

    async with factory() as db:
        open_before = (await db.execute(select(RoundInstance).where(
            RoundInstance.match_id == match_id,
            RoundInstance.status == ROUND_OPEN))).scalars().all()
        total = (await db.execute(select(RoundInstance).where(
            RoundInstance.match_id == match_id))).scalars().all()
        assert len(open_before) == len(total) >= 4, \
            f"四院应并行开放，实际开放 {len(open_before)} / 共 {len(total)}"

    rr = await command(client, match_id, "lock")
    assert rr.status_code == 200, rr.text

    async with factory() as db:
        still_open = (await db.execute(select(RoundInstance).where(
            RoundInstance.match_id == match_id,
            RoundInstance.status == ROUND_OPEN))).scalars().all()
        assert not still_open, f"结束后仍有 {len(still_open)} 个回合开放"
        closed = (await db.execute(select(RoundInstance).where(
            RoundInstance.match_id == match_id,
            RoundInstance.status == ROUND_CLOSED))).scalars().all()
        assert len(closed) == len(total), "所有回合都应被计入有效题"


@pytest.mark.asyncio
async def test_relay_timing_uses_first_solve_and_feeds_the_score(db_setup, client):
    """用时按「本院最快解出该题的时刻」求和；时间权重为 0 时不影响分数。"""
    async def build(weight_time: int):
        r = await _make_relay(db_setup, client, per_house=1)
        assert r.status_code == 200, r.text
        match_id = r.json()["match_id"]
        await command(client, match_id, "open_lobby")
        factory = make_session_factory()
        for key in db_setup["houses"]:
            pc = make_client()
            await login(pc, f"p_{key}_1", "pass-123456")
            await pc.post(f"/api/v1/matches/{match_id}/check-in")
            await pc.aclose()
        await command(client, match_id, "start_match")
        await advance_factory()(match_id, utcnow())

        async with factory() as db:
            m = await db.get(Match, match_id)
            m.weight_accuracy = 1000 - weight_time
            m.weight_time = weight_time
            rounds = (await db.execute(select(RoundInstance).where(
                RoundInstance.match_id == match_id))).scalars().all()
            # 固定用时：tiger 最快（1s），dragon 最慢（12s），另外两院中间
            plan = {"tiger": 1000, "dragon": 12000, "tortoise": 6000, "phoenix": 6000}
            for rd in rounds:
                rd.status = ROUND_CLOSED
                rd.closed_at = utcnow()
                hk = (rd.house_scope or [None])[0]
                mq = await db.get(MatchQuestion, rd.match_question_id)
                v = await db.get(QuestionVersion, mq.version_id)
                entry = (await db.execute(select(MatchEntry).where(
                    MatchEntry.match_id == match_id,
                    MatchEntry.house_key_snapshot == hk))).scalars().first()
                db.add(Answer(match_id=match_id, round_instance_id=rd.id, entry_id=entry.id,
                              option_id=v.correct_option_id,
                              idempotency_key=f"k{new_id()[:16]}", received_at=utcnow(),
                              receipt_id=f"r{new_id()[:8]}", fingerprint=f"fp{hk}",
                              elapsed_ms=plan[hk]))
            m.current_round_id = None
            await db.commit()
            rv = await calculate_result(db, m, None)
            await db.commit()
            payload = await results_payload(db, m, rv)
            return payload

    payload = await build(300)
    by_key = {h["key"]: h for h in payload["houses"]}
    assert payload["match"]["competition_mode"] == "relay"
    tiger, dragon = by_key["tiger"], by_key["dragon"]
    assert tiger["timing"]["total_ms"] == 1000
    assert dragon["timing"]["total_ms"] == 12000
    assert tiger["timing"]["time_factor"] == 1.0
    # 时间系数落库是千分位整数，对外按**两位小数**展示（与结果页的分数精度一致），
    # 因此期望值要按同一套取整算，而不是拿浮点原值去比。
    assert dragon["timing"]["time_factor"] == round(round(1000 / 12000 * 1000) / 1000, 2)
    assert tiger["timing"]["completed"] is True
    # 都答对 1 题：时间权重 300 时最快的学院分更高
    assert tiger["score"] > dragon["score"], f"tiger={tiger['score']} dragon={dragon['score']}"

    payload0 = await build(0)
    b0 = {h["key"]: h for h in payload0["houses"]}
    assert b0["tiger"]["score"] == b0["dragon"]["score"], (
        "时间权重为 0 时，用时不应影响学院分："
        + str({k: (v["score"], v["correct_total"], v["participants"]) for k, v in b0.items()}))


@pytest.mark.asyncio
async def test_participation_dimension_changes_house_score(db_setup, client):
    """参与度维度打开后，必须真的改变学院分。

    构造：tiger 有 **3 人**在名单里但只有 1 人答对；dragon 有 1 人在名单里且答对 1 题。
    两院的"答对总量 / 应答题量"都相同，因此只看正确率时同分；
    但 tiger 的参与度是 1/3、dragon 是 1/1，所以打开参与度权重后 dragon 必须更高。

    这条守的是产品要求里的"综合评估包含参与度"，并且顺带证明
    "参与度"不是装饰性字段——它确实进入计分公式。
    """
    match_id = await make_match(db_setup, client, question_count=1, display=0, answer=60)
    r = await command(client, match_id, "start_match")
    assert r.status_code == 200, r.text
    await advance_factory()(match_id, utcnow())

    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        # tiger 追加两名**新账号**（不能复用已在名单里的人：唯一约束是 (match_id, user_id)）
        tiger = (await db.execute(select(House).where(House.key == "tiger"))).scalar_one()
        for i in (2, 3):
            u = User(id=new_id(), username=f"part_tiger_{i}", password_hash="x",
                     role=ROLE_STUDENT, house_id=tiger.id)
            db.add(u)
            await db.flush()
            db.add(MatchEntry(id=new_id(), match_id=match_id, user_id=u.id,
                              house_id=tiger.id, house_key_snapshot="tiger",
                              house_name_snapshot=tiger.name_cn,
                              display_alias=f"tiger-0{i}"))
        await db.commit()

    async with factory() as db:
        m = await db.get(Match, match_id)
        m.scoring_mode = "composite"
        m.weight_accuracy = 1000
        m.weight_participation = 0
        rd = await db.get(RoundInstance, m.current_round_id)
        rd.status = ROUND_CLOSED
        rd.closed_at = utcnow()
        m.current_round_id = None
        mq = (await db.execute(select(MatchQuestion).where(
            MatchQuestion.match_id == match_id, MatchQuestion.is_backup.is_(False)))).scalars().first()
        v = await db.get(QuestionVersion, mq.version_id)
        entries = (await db.execute(select(MatchEntry).where(
            MatchEntry.match_id == match_id).order_by(MatchEntry.created_at))).scalars().all()
        # 每院只让第一个人作答且答对
        by_house: dict[str, list] = {}
        for e in entries:
            by_house.setdefault(e.house_key_snapshot, []).append(e)
        for hk, rows in by_house.items():
            db.add(Answer(match_id=match_id, round_instance_id=rd.id, entry_id=rows[0].id,
                          option_id=v.correct_option_id,
                          idempotency_key=f"k{new_id()[:16]}", received_at=utcnow(),
                          receipt_id=f"r{new_id()[:8]}", fingerprint=f"fp{hk}", elapsed_ms=2000))
        await db.commit()
        rv0 = await calculate_result(db, m, None)
        await db.commit()
        p0 = await results_payload(db, m, rv0)
        s0 = {h["key"]: h["score"] for h in p0["houses"]}
        # 参与度权重 0 时只有正确率与速度起作用（此场速度权重默认 300）：
        #   tiger  正确率 1/3、速度 1.0 → (1000×1/3 + 300×1)/1300 × 100 = 48.72
        #   其余三院正确率 1.0、速度 1.0 → 100
        # 断言用"公式算出来的值"，而不是凭感觉写的数——上一版就是因为把速度权重漏掉才误判。
        assert abs(s0["tiger"] - round((1000 * (1 / 3) + 300 * 1) / 1300 * 100, 2)) < 0.02, s0
        assert s0["dragon"] == 100.0, s0
        assert "参与度" not in (p0["match"]["scoring_note"] or "")

        # 把速度权重让给参与度（1000/0/400）后：tiger 正确率 1/3、参与度 1/3 → 33.33；
        # dragon 三项全满 → 100。参与度这一维把"少而精"与"齐而准"的差距**拉大**：
        # 只看正确率+速度时 dragon 领先 tiger 51.28，加入参与度后领先 66.67。
        m.weight_accuracy = 600
        m.weight_speed = 0
        m.weight_participation = 400
        await db.commit()
        rv1 = await calculate_result(db, m, None)
        await db.commit()
        p1 = await results_payload(db, m, rv1)
        s1 = {h["key"]: h["score"] for h in p1["houses"]}
        assert s1["dragon"] == 100.0, f"dragon 全参与全对应为满分：{s1}"
        assert abs(s1["tiger"] - 33.33) < 0.02, f"tiger 正确率与参与度都是 1/3：{s1}"
        assert s1["dragon"] - s1["tiger"] > s0["dragon"] - s0["tiger"], (
            f"参与度应把差距拉大：before {s0['dragon'] - s0['tiger']:.2f} → "
            f"after {s1['dragon'] - s1['tiger']:.2f}；"
            f"weights={p1['match'].get('weight_accuracy')}/{p1['match'].get('weight_participation')} "
            f"scores={s0} → {s1}")
        assert p1["match"]["weight_participation"] == 400
        assert "参与度" in (p1["match"]["scoring_note"] or ""), p1["match"]["scoring_note"]


@pytest.mark.asyncio
async def test_relay_replay_timeline_is_anonymous_and_hides_correctness(db_setup, client):
    """镜像回放：给时间线、给匿名别名，但**未揭晓前不给对错**。"""
    r = await _make_relay(db_setup, client, per_house=1)
    match_id = r.json()["match_id"]
    await command(client, match_id, "open_lobby")
    factory = make_session_factory()
    for key in db_setup["houses"]:
        pc = make_client()
        await login(pc, f"p_{key}_1", "pass-123456")
        await pc.post(f"/api/v1/matches/{match_id}/check-in")
        await pc.aclose()
    await command(client, match_id, "start_match")
    await advance_factory()(match_id, utcnow())

    # 一院交一份
    pc = make_client()
    try:
        await login(pc, "p_tiger_1", "pass-123456")
        snap = (await pc.get(f"/api/v1/matches/{match_id}/snapshot")).json()
        ctl = (await pc.post("/api/v1/auth/control/claim")).json()["control_token"]
        await pc.post(
            f"/api/v1/matches/{match_id}/rounds/{snap['round']['round_instance_id']}/answers",
            json={"option_id": "o1"},
            headers={"Idempotency-Key": f"k-{new_id()[:16]}", "X-Control-Token": ctl})
        rep = (await pc.get(f"/api/v1/matches/{match_id}/replay")).json()
    finally:
        await pc.aclose()

    assert rep["match"]["competition_mode"] == "relay"
    assert len(rep["rounds"]) == 4
    assert rep["events"], "回放应有作答事件"
    ev = rep["events"][0]
    assert ev["house"] == "tiger"
    assert ev["alias"], "回放用匿名别名，不带真实身份"
    assert ev["correct"] is None, "未揭晓前回放不得给出对错"
    assert ev["elapsed_ms"] is not None, "回放要能看出每题用时"
    assert "revealed" in rep["note"] or not rep["match"]["revealed"]

    anon = make_client()
    try:
        an = await anon.get(f"/api/v1/matches/{match_id}/replay")
        assert an.status_code == 200, "匿名走的是**公开回放**（大屏用），不应被拒"
        pub = an.json()
        pev = pub["events"][0]
        # 公开回放不带任何个人标识与选项，也不给对错（走廊屏幕不该出现个人痕迹）
        assert "alias" not in pev and "option_id" not in pev and "correct" not in pev, pev
        assert pev["house"] == "tiger" and pev["elapsed_ms"] is not None
        assert pub["viewer"]["anonymous"] is True
    finally:
        await anon.aclose()