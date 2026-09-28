"""B2：匿名练习与排行榜。

覆盖面：
- 匿名访客可练习（无登录）、可上榜，展示名为「枫禾学子 #nn」且不含任何真实信息
- 答题幂等；服务端自算用时；单题 < 0.4s 不计入速度但计入正确率
- 一场练习只结算一次（best_streak / total_ms / is_ranked）
- 排行榜只输出展示名，不输出账号名/真实姓名；门槛过滤生效
- 练习成绩与正式赛成绩物理分区（practice_* 与 matches/answers 不通）
"""
import uuid

from sqlalchemy import func, select

from app.core.security import new_id
from app.db.base import utcnow
from app.db.models import (
    Answer,
    House,
    Match,
    PracticeAnswer,
    PracticeSession,
    Question,
    QuestionVersion,
    User,
    MATCH_REVEALED,
    POOL_PRACTICE,
    ROLE_STUDENT,
)
from app.db.session import make_session_factory
from app.modules.practice.identity import ANONYMOUS_NAME
from tests.conftest import login, make_client


async def _seed_practice_questions(n: int, subject: str = "科学常识") -> list[str]:
    """建 n 道练习池题（draft/pending 状态 —— 正是"开箱即可练"要覆盖的情形）。"""
    factory = make_session_factory()
    vids = []
    async with factory() as db:
        for i in range(n):
            q = Question(id=new_id(), subject=subject, difficulty=1, language="zh",
                         pool=POOL_PRACTICE, status="draft", prompt_hash=f"ph-{uuid.uuid4().hex}")
            v = QuestionVersion(id=new_id(), question_id=q.id, version_no=1,
                                prompt=f"练习题 {i}？",
                                options_json=[{"option_id": "o1", "text": "对"},
                                              {"option_id": "o2", "text": "错"}],
                                correct_option_id="o1", review_state="pending",
                                explanation=f"这是第 {i} 题的解析。")
            db.add_all([q, v])
            q.current_version_id = v.id
            vids.append(v.id)
        await db.commit()
    return vids


async def _pool_and_subject_of(vids: list[str]) -> dict[str, tuple[str, str]]:
    """题目版本 id → (池, 主题)。用来直接问库，而不是相信接口自报。"""
    factory = make_session_factory()
    async with factory() as db:
        rows = (await db.execute(
            select(QuestionVersion.id, Question.pool, Question.subject)
            .join(Question, Question.current_version_id == QuestionVersion.id)
            .where(QuestionVersion.id.in_(vids)))).all()
    return {vid: (pool, subject) for vid, pool, subject in rows}


# ── 匿名可练 ────────────────────────────────────────────────────────────────

async def test_anonymous_can_practice_without_login(db_setup):
    """匿名访客（无会话 Cookie）也能建练习会话 —— 首访自动签发访客身份。"""
    await _seed_practice_questions(5)
    anon = make_client()
    r = await anon.post("/api/v1/practice/sessions", json={"count": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["anonymous"] is True
    assert len(body["questions"]) == 3
    assert anon.cookies.get("mgus_guest"), "首访必须下发访客 Cookie"
    # 展示名必须是匿名默认名，且**不得**包含任何账号/姓名
    assert body["learner"].startswith(ANONYMOUS_NAME), body["learner"]
    # 主题与难度必须随题目一起返回（曾经因为字典键用错而恒为 null）
    assert all(q.get("subject") for q in body["questions"]), \
        f"题目缺少主题：{[q.get('subject') for q in body['questions']]}"
    assert all(q.get("difficulty") in (1, 2, 3) for q in body["questions"]), \
        f"题目缺少难度：{[q.get('difficulty') for q in body['questions']]}"
    await anon.aclose()


async def test_model_and_migrations_agree_on_nullability(db_setup):
    """模型声明与迁移结果必须一致。

    为什么单独测这个：测试库是用 `Base.metadata.create_all` 按**当前模型**建的，
    所以「模型改成 nullable、物理库仍是 NOT NULL」这类偏差在测试里永远看不见——
    而它在真实库上会直接 500（本项目实际踩过一次：b2 迁移漏了 user_id 的 ALTER）。
    这里对真实测试库做结构断言，作为模型 ↔ 迁移一致性的哨兵。
    """
    from sqlalchemy import inspect as sa_inspect

    from app.db.models import PracticeSession

    def _check(sync_conn):
        insp = sa_inspect(sync_conn)
        live = {c["name"]: c for c in insp.get_columns(PracticeSession.__tablename__)}
        wanted = {c.name: c.nullable for c in PracticeSession.__table__.columns}
        mismatched = [
            (name, "model nullable=%s" % wanted[name], "db nullable=%s" % live[name]["nullable"])
            for name in wanted
            if name in live and bool(wanted[name]) != bool(live[name]["nullable"])
        ]
        missing = [n for n in wanted if n not in live]
        return mismatched, missing

    from app.db.session import get_engine
    engine = get_engine()
    async with engine.connect() as conn:
        mismatched, missing = await conn.run_sync(_check)
    assert not missing, f"迁移缺少字段：{missing}"
    assert not mismatched, f"可空性与模型不一致（模型/库）：{mismatched}"


async def test_anonymous_guest_seq_is_stable_across_requests(db_setup):
    anon = make_client()
    first = (await anon.post("/api/v1/practice/sessions", json={"count": 1})).json()
    second = (await anon.post("/api/v1/practice/sessions", json={"count": 1})).json()
    assert first["learner"] == second["learner"], "同一访客的序号必须稳定"
    await anon.aclose()


async def test_anonymous_answer_flow_and_server_side_timing(db_setup):
    await _seed_practice_questions(5)
    anon = make_client()
    s = (await anon.post("/api/v1/practice/sessions", json={"count": 2})).json()
    sid = s["session_id"]
    qv = s["questions"][0]["question_version_id"]

    r = await anon.post(f"/api/v1/practice/sessions/{sid}/answers",
                        json={"question_version_id": qv, "option_id": "o1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_correct"] is True
    assert body["correct_option_id"] == "o1"
    assert body["explanation"]
    # 用时不信任客户端：即使客户端谎报 5ms，服务端仍按自己的受理时间算。
    assert body["elapsed_ms"] >= 0
    # "低于 0.4 秒不计入速度榜"这条规则本身由下面的显式等待来验证 ——
    # 不再断言"首答一定很快"：那依赖机器负载，在并行跑测试时会假失败。
    assert body["counted_for_speed"] == (body["elapsed_ms"] >= 400)
    assert body["summary"]["correct_count"] == 1

    # 显式等 1.2 秒再答第二题：服务端用时必须真的把这段等待算进去。
    #
    # 为什么是 1.2 秒而不是 0.5 秒：本系统的时间列统一按**整秒**存
    # （`db.base.utcnow()` 会抹掉微秒，与库内 naive-UTC 约定配套），
    # 所以服务端算出来的用时天然是 1000 的整数倍。等 0.5 秒时两次受理可能落在同一秒里，
    # 用时就是 0 —— 那是时间粒度造成的，不是计时逻辑错了。等满一秒以上才可判定。
    # 第二题刻意答错（o2），这样上面"答对 1 题"的计数断言仍然成立。
    import asyncio as _asyncio
    await _asyncio.sleep(1.2)
    qv2 = s["questions"][1]["question_version_id"]
    r_slow = await anon.post(f"/api/v1/practice/sessions/{sid}/answers",
                             json={"question_version_id": qv2, "option_id": "o2",
                                   "client_elapsed_ms": 5})
    assert r_slow.status_code == 200, r_slow.text
    assert r_slow.json()["elapsed_ms"] >= 1000, r_slow.text
    assert r_slow.json()["counted_for_speed"] is True, r_slow.text

    # 幂等：重复提交同题返回原结果，不重复计数
    r2 = await anon.post(f"/api/v1/practice/sessions/{sid}/answers",
                         json={"question_version_id": qv, "option_id": "o2"})
    assert r2.status_code == 200
    assert r2.json()["already_answered"] is True
    assert r2.json()["is_correct"] is True, "重复提交不得改写原答案"
    assert r2.json()["summary"]["correct_count"] == 1
    await anon.aclose()


async def test_guest_cannot_read_another_guests_session(db_setup):
    await _seed_practice_questions(3)
    a, b = make_client(), make_client()
    sa = (await a.post("/api/v1/practice/sessions", json={"count": 1})).json()
    r = await b.get(f"/api/v1/practice/sessions/{sa['session_id']}")
    assert r.status_code == 404, "不同访客之间必须互相看不到会话"
    await a.aclose(); await b.aclose()


# ── 多主题 ──────────────────────────────────────────────────────────────────

async def test_multi_subject_only_draws_picked_subjects_and_only_from_practice_pool(db_setup):
    """多主题练习：只出勾选主题的题，且**只能**来自练习池。

    为什么要把"池"也断言一遍：练习池与正式池的物理隔离是红线
    （competitions 只查 pool=official，练习只查 pool=practice）。
    conftest 里预置了 6 道 official 池、主题同为「科学常识」的已发布题，
    所以"勾了科学常识"正好是最容易越界的一种请求 —— 如果过滤条件里漏掉
    `_PRACTICE_USABLE`，正式题会立刻混进练习题里。
    """
    await _seed_practice_questions(4, subject="地理文化")
    await _seed_practice_questions(4, subject="科学常识")
    anon = make_client()
    r = await anon.post("/api/v1/practice/sessions",
                        json={"subjects": ["科学常识", "地理文化"], "count": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subjects"] == ["科学常识", "地理文化"], body["subjects"]

    vids = [q["question_version_id"] for q in body["questions"]]
    assert len(vids) == 5
    where = await _pool_and_subject_of(vids)
    assert len(where) == 5, f"有题目查不到所属题：{vids}"
    pools = {pool for pool, _ in where.values()}
    assert pools == {POOL_PRACTICE}, f"练习抽到了非练习池的题：{where}"
    assert {sub for _, sub in where.values()} <= {"科学常识", "地理文化"}, where
    # 会话里的 subject 是单值列：多主题时为空，范围由 subjects 交代。
    sess = (await anon.get(f"/api/v1/practice/sessions/{body['session_id']}")).json()
    assert sess["subject"] is None
    assert set(sess["subjects"]) <= {"科学常识", "地理文化"} and sess["subjects"], sess["subjects"]
    await anon.aclose()


async def test_single_subject_keeps_legacy_semantics_and_empty_list_means_all(db_setup):
    """单选仍是单选（GET 的 subject 语义不变）；空数组＝全部主题。"""
    await _seed_practice_questions(3, subject="地理文化")
    await _seed_practice_questions(3, subject="科学常识")
    anon = make_client()

    one = (await anon.post("/api/v1/practice/sessions",
                           json={"subjects": ["地理文化"], "count": 2})).json()
    assert one["subjects"] == ["地理文化"]
    got = (await anon.get(f"/api/v1/practice/sessions/{one['session_id']}")).json()
    assert got["subject"] == "地理文化" and got["subjects"] == ["地理文化"], got["subject"]
    where = await _pool_and_subject_of([q["question_version_id"] for q in one["questions"]])
    assert {sub for _, sub in where.values()} == {"地理文化"}, where

    # 空数组与"一个都不给"等价：不限主题。回显的是实际抽到的主题，不是空数组。
    empty = await anon.post("/api/v1/practice/sessions", json={"subjects": [], "count": 4})
    assert empty.status_code == 200, empty.text
    assert empty.json()["subjects"], "不限主题时应回显实际抽到的主题"
    none = await anon.post("/api/v1/practice/sessions", json={"count": 4})
    assert none.status_code == 200, none.text

    # 旧调用方只传 subject：必须与"勾一个主题"完全等价，
    # 尤其 **subject 要照旧落库** —— 否则 GET 的 subject 字段会从主题名变成 null，
    # 老前端与老脚本会突然读不到自己练的是哪个主题。
    legacy = (await anon.post("/api/v1/practice/sessions",
                              json={"subject": "地理文化", "count": 2})).json()
    legacy_get = (await anon.get(f"/api/v1/practice/sessions/{legacy['session_id']}")).json()
    assert legacy_get["subject"] == "地理文化", legacy_get
    assert legacy_get["subjects"] == ["地理文化"], legacy_get["subjects"]
    await anon.aclose()


async def test_subject_input_is_cleaned_deduped_and_capped(db_setup):
    """空串/重复项被清洗；超过主题总数明确报 422，而不是悄悄截断。"""
    await _seed_practice_questions(3, subject="地理文化")
    anon = make_client()
    cleaned = await anon.post("/api/v1/practice/sessions",
                              json={"subjects": [" 地理文化 ", "", "   ", "地理文化"], "count": 2})
    assert cleaned.status_code == 200, cleaned.text
    assert cleaned.json()["subjects"] == ["地理文化"], cleaned.json()["subjects"]

    too_many = await anon.post("/api/v1/practice/sessions",
                               json={"subjects": [f"主题{i}" for i in range(12)], "count": 1})
    assert too_many.status_code == 422, too_many.text
    assert too_many.json()["code"] == "VALIDATION_ERROR", too_many.text

    # 勾了主题但该主题一道可练的题都没有 → 沿用既有那句 404，不新造错误码。
    missing = await anon.post("/api/v1/practice/sessions",
                              json={"subjects": ["不存在的主题"], "count": 3})
    assert missing.status_code == 404, missing.text
    assert missing.json()["code"] == "PRACTICE_ERROR", missing.text
    await anon.aclose()


async def test_subjects_endpoint_separates_total_from_published(db_setup):
    """题库总数（含待审核）与已审核数必须分开给出，且 total 的口径不能变。"""
    await _seed_practice_questions(3, subject="地理文化")   # draft：可练但未审核
    anon = make_client()
    body = (await anon.get("/api/v1/practice/subjects")).json()
    assert "total" in body and "published" in body, body
    assert body["published"] <= body["total"], body

    factory = make_session_factory()
    async with factory() as db:
        rows = (await db.execute(
            select(Question.status, func.count()).select_from(Question)
            .where(Question.pool == POOL_PRACTICE, Question.status != "retired")
            .group_by(Question.status))).all()
    expect_total = sum(c for _, c in rows)
    expect_pub = dict(rows).get("published", 0)
    assert body["total"] == expect_total, (body, rows)
    assert body["published"] == expect_pub, (body, rows)
    assert body["published"] < body["total"], "本用例故意留了 draft 题，两个数不应相等"
    await anon.aclose()


# ── 结算 ────────────────────────────────────────────────────────────────────

async def test_finalize_is_computed_once(db_setup):
    """一场练习结束时结算：最长连对、总用时、入榜判定；重复调用不改变结果。"""
    vids = await _seed_practice_questions(4)
    from app.modules.practice.router import _finalize

    factory = make_session_factory()
    async with factory() as db:
        sess = PracticeSession(id=new_id(), guest_id=None, user_id=None,
                               question_ids=vids, subject="科学常识")
        db.add(sess)
        await db.flush()
        # 对 对 错 对 → 最长连对 2
        for i, correct in enumerate([True, True, False, True]):
            db.add(PracticeAnswer(session_id=sess.id, question_version_id=vids[i],
                                  option_id="o1" if correct else "o2",
                                  is_correct=correct, answered_at=utcnow(),
                                  elapsed_ms=1200 + i * 100))
        await db.flush()
        await _finalize(db, sess)
        first = (sess.best_streak, sess.answered_count, sess.correct_count, sess.total_ms,
                 sess.is_ranked)
        await _finalize(db, sess)     # 第二次必须幂等（题量不足门槛 → 不入榜）
        second = (sess.best_streak, sess.answered_count, sess.correct_count, sess.total_ms,
                  sess.is_ranked)
        await db.commit()
    assert first == second
    assert first[0] == 2, f"最长连对应为 2，实际 {first[0]}"
    assert first[1] == 4 and first[2] == 3
    assert first[3] == 1200 + 1300 + 1400 + 1500
    assert first[4] is False, "4 题不应达到入榜门槛"


# ── 排行榜 ──────────────────────────────────────────────────────────────────

async def _make_ranked_session(correct: int, answered: int, streak: int, total_ms: int | None,
                               display: str | None, house_id: str | None = None,
                               user_id: str | None = None) -> str:
    factory = make_session_factory()
    async with factory() as db:
        s = PracticeSession(id=new_id(), user_id=user_id, guest_id=None, house_id=house_id,
                            question_ids=[], status="finished", answered_count=answered,
                            correct_count=correct, score_num=correct * 10000, score_den=answered,
                            best_streak=streak, total_ms=total_ms, is_ranked=True,
                            display_name=display, finished_at=utcnow())
        db.add(s)
        await db.commit()
        return s.id


async def test_leaderboard_is_public_and_never_leaks_identity(db_setup):
    """排行榜无需登录；只输出展示名；匿名者显示「枫禾学子」。"""
    await _make_ranked_session(correct=22, answered=25, streak=9, total_ms=40_000,
                               display=None)
    await _make_ranked_session(correct=18, answered=25, streak=4, total_ms=30_000,
                               display="爱答题的同学")
    anon = make_client()
    r = await anon.get("/api/v1/leaderboard?board=accuracy&limit=20")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["min_questions"] == 20
    names = [it["display_name"] for it in body["items"]]
    assert all(n for n in names), "展示名不得为空"
    assert any(n.startswith(ANONYMOUS_NAME) for n in names), f"匿名者应显示默认名：{names}"
    # 不得出现任何账号名或 user_id 字段
    raw = r.text
    for banned in ("username", "user_id", "display_name\":null", "demo_tiger"):
        assert banned not in raw, f"公开榜泄露了 {banned}"
    # 排序：正确率高者在前
    acc = [it["accuracy"] for it in body["items"]]
    assert acc == sorted(acc, reverse=True), f"正确率榜排序错误：{acc}"
    await anon.aclose()


async def test_leaderboard_threshold_filters_small_samples(db_setup):
    """低于门槛的成绩不得出现在榜上（防止 3 题 100% 霸榜）。"""
    factory = make_session_factory()
    async with factory() as db:
        s = PracticeSession(id=new_id(), question_ids=[], status="finished",
                            answered_count=3, correct_count=3, best_streak=3,
                            is_ranked=False, display_name=None)
        db.add(s)
        await db.commit()
    anon = make_client()
    body = (await anon.get("/api/v1/leaderboard?board=accuracy&limit=100")).json()
    assert all(it["answered_count"] >= 20 for it in body["items"])
    await anon.aclose()


async def test_leaderboard_house_board_does_not_claim_official_points(db_setup):
    """House 荣誉榜必须明确声明不计入官方积分（学生最容易误解的一处）。"""
    tiger = db_setup["houses"]["tiger"]
    u = User(id=new_id(), username=f"lb_{uuid.uuid4().hex[:6]}",
             password_hash="x", role=ROLE_STUDENT, house_id=tiger.id)
    factory = make_session_factory()
    async with factory() as db:
        db.add(u)
        await db.commit()
    await _make_ranked_session(correct=20, answered=25, streak=5, total_ms=20_000,
                               display=None, house_id=tiger.id, user_id=u.id)
    anon = make_client()
    body = (await anon.get("/api/v1/leaderboard?board=house")).json()
    assert "不计入" in body["note"], body["note"]
    tiger_row = next((r for r in body["items"] if r["house_key"] == "tiger"), None)
    assert tiger_row is not None and tiger_row["answered_total"] >= 25
    await anon.aclose()


async def test_practice_scores_stay_in_their_own_tables(db_setup):
    """物理分区：练习结算不得写入正式赛的 matches/answers 表。"""
    vids = await _seed_practice_questions(2)
    from app.modules.practice.router import _finalize
    factory = make_session_factory()
    async with factory() as db:
        before_matches = len((await db.execute(select(Match.id))).all())
        before_answers = len((await db.execute(select(Answer.id))).all())
        sess = PracticeSession(id=new_id(), question_ids=vids)
        db.add(sess)
        await db.flush()
        for i in range(2):
            db.add(PracticeAnswer(session_id=sess.id, question_version_id=vids[i],
                                  option_id="o1", is_correct=True, answered_at=utcnow(),
                                  elapsed_ms=900))
        await db.flush()
        await _finalize(db, sess)
        await db.commit()
        after_matches = len((await db.execute(select(Match.id))).all())
        after_answers = len((await db.execute(select(Answer.id))).all())
    assert after_matches == before_matches
    assert after_answers == before_answers
