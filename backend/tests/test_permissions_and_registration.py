"""四条产品红线 + 观战三档 + 自助注册 / House 归属 / 封禁。

红线（服务端强制，前端隐藏按钮不算）：
  R1 参赛账号必须已绑定 House（否则会产出不计分的"幽灵选手"）
  R2 参赛与快照需要登录并绑定 House；公开观战为只读分层视图，不返回答案
  R3 未揭晓前，不属于本场的学院看不到本场
  R4 选手只能加入包含自己学院的那一场比赛

以及：
  · 自助注册：House 自选并锁定，本人无通路更改；开关关闭时拒绝
  · 管理员可调整 House、封禁账号（封禁必须**立即**踢下线）
  · 观战三档：mirror 给题面、blur/summary 不给题面，**任何档位都不给标准答案**
"""
import uuid

from sqlalchemy import select

from app.core.security import hash_password, new_id
from app.db.models import (
    Answer,
    House,
    Match,
    MatchEntry,
    Question,
    QuestionVersion,
    RoundInstance,
    User,
    UserSession,
    ROLE_ADMIN,
    ROLE_STUDENT,
)
from app.db.session import make_session_factory
from tests.conftest import command, login, make_client, make_match


async def _mk_user(username: str, role: str, house_key: str | None,
                   password: str = "pass-123456") -> str:
    factory = make_session_factory()
    async with factory() as db:
        hid = None
        if house_key:
            h = (await db.execute(select(House).where(House.key == house_key))).scalar_one()
            hid = h.id
        u = User(id=new_id(), username=username, password_hash=hash_password(password),
                 role=role, house_id=hid, status="active", must_change_password=False)
        db.add(u)
        await db.commit()
        return u.id


# ── R1 / R2：必须登录、必须有 House ─────────────────────────────────────────

async def test_no_house_account_cannot_check_in_or_submit(db_setup):
    """无 House 的账号：签到与参赛快照被拒；公开观战仍可看，但拿不到答案。"""
    uname = f"nohouse_{uuid.uuid4().hex[:6]}"
    await _mk_user(uname, ROLE_STUDENT, None)
    match_id = await make_match(db_setup, await _admin_client(), question_count=1, fixed_k=1,
                                with_roster=False)
    c = make_client()
    await login(c, uname, "pass-123456")
    for path, method in (("check-in", "post"), ("snapshot", "get")):
        r = await getattr(c, method)(f"/api/v1/matches/{match_id}/{path}")
        assert r.status_code == 403, f"{path}: {r.text}"
        assert r.json()["code"] == "HOUSE_REQUIRED", f"{path}: {r.text}"
        assert r.json()["details"].get("needs_house") is True
    r = await c.get(f"/api/v1/matches/{match_id}/spectate")
    assert r.status_code == 200, r.text
    assert r.json()["viewer"]["anonymous"] is False
    assert r.json()["viewer"]["house_key"] is None
    assert "correct_option_id" not in r.text and "explanation" not in r.text
    await c.aclose()


async def test_anonymous_gets_public_spectate_but_not_snapshot_or_check_in(db_setup):
    match_id = await make_match(db_setup, await _admin_client(), question_count=1, fixed_k=1,
                                with_roster=False)
    anon = make_client()
    r = await anon.get(f"/api/v1/matches/{match_id}/snapshot")
    assert r.status_code == 401, r.text
    assert r.json()["code"] == "LOGIN_REQUIRED"
    for path in ("spectate", "spectate/live"):
        r = await anon.get(f"/api/v1/matches/{match_id}/{path}")
        assert r.status_code == 200, f"{path}: {r.text}"
        assert r.json()["viewer"]["anonymous"] is True
        assert "correct_option_id" not in r.text and "explanation" not in r.text
    r = await anon.post(f"/api/v1/matches/{match_id}/check-in")
    assert r.status_code == 401
    await anon.aclose()


async def _admin_client():
    c = make_client()
    await login(c, "admin", "admin-pass-123")
    return c


# ── R3 / R4：限定学院范围 ───────────────────────────────────────────────────

async def _mk_scoped_match(houses: list[str], question_count: int = 1) -> str:
    """建一场只允许指定学院参加的比赛（houses_scope）。"""
    factory = make_session_factory()
    async with factory() as db:
        vids = [vid for (vid,) in (await db.execute(
            select(QuestionVersion.id).join(
                Question, Question.current_version_id == QuestionVersion.id)
        )).all()][:question_count]
        m = Match(id=new_id(), title=f"[DEMO] 限定赛 {uuid.uuid4().hex[:4]}",
                  mode="open", status="lobby", state_version=1,
                  planned_questions=question_count, min_house_size=1,
                  houses_scope=houses)
        db.add(m)
        await db.flush()
        from app.db.models import MatchQuestion
        for i, vid in enumerate(vids, start=1):
            q = (await db.execute(select(QuestionVersion).where(QuestionVersion.id == vid))).scalar_one()
            db.add(MatchQuestion(id=new_id(), match_id=m.id, question_id=q.question_id,
                                 version_id=vid, position=i, is_backup=False))
        await db.commit()
        return m.id


async def test_outsider_cannot_check_in_scoped_match(db_setup):
    """R4：限定 tiger/dragon 的比赛，phoenix 选手签到被拒。"""
    match_id = await _mk_scoped_match(["tiger", "dragon"])
    outsider = f"outsider_{uuid.uuid4().hex[:6]}"
    await _mk_user(outsider, ROLE_STUDENT, "phoenix")
    c = make_client()
    await login(c, outsider, "pass-123456")
    r = await c.post(f"/api/v1/matches/{match_id}/check-in")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "NOT_YOUR_HOUSE"
    assert r.json()["details"]["scope"] == ["tiger", "dragon"]
    await c.aclose()


async def test_outsider_cannot_spectate_scoped_match_before_reveal(db_setup):
    """R3：未揭晓前，范围外学院连观战接口都打不开。"""
    match_id = await _mk_scoped_match(["tiger", "dragon"])
    outsider = f"spec_{uuid.uuid4().hex[:6]}"
    await _mk_user(outsider, ROLE_STUDENT, "phoenix")
    c = make_client()
    await login(c, outsider, "pass-123456")
    for path in ("spectate", "spectate/live"):
        r = await c.get(f"/api/v1/matches/{match_id}/{path}")
        assert r.status_code == 403, f"{path}: {r.text}"
        assert r.json()["code"] == "NOT_YOUR_HOUSE"
    # 揭晓后放开
    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        m.status = "revealed"
        await db.commit()
    r = await c.get(f"/api/v1/matches/{match_id}/spectate/live")
    assert r.status_code == 200, r.text
    await c.aclose()


async def test_insider_can_check_in_scoped_match(db_setup):
    """R4 正例：范围内的学院可以正常签到，且返回本院信息。

    **刻意不预写任何资格记录**：参赛资格已改为"绑定学院即可"，
    这条用例就是防止"资格门"以任何形式再回来。
    """
    match_id = await _mk_scoped_match(["tiger", "dragon"])
    insider = f"insider_{uuid.uuid4().hex[:6]}"
    await _mk_user(insider, ROLE_STUDENT, "dragon")
    c = make_client()
    await login(c, insider, "pass-123456")
    r = await c.post(f"/api/v1/matches/{match_id}/check-in")
    assert r.status_code == 200, r.text
    assert r.json()["house_key"] == "dragon"
    await c.aclose()


async def test_plain_student_can_check_in_without_any_eligibility(db_setup):
    """任何绑定学院的账号都能参赛 —— 不需要角色、也不需要管理员单独加资格。

    这是产品明确要求去掉的机制：过去 student 只能练习，entrant 才能进名单，
    而且还要再点一次"加资格"。现在签到只看"账号有没有绑定本场范围内的学院"。
    """
    match_id = await make_match(db_setup, await _admin_client(), question_count=1, fixed_k=1,
                                with_roster=False)
    # `with_roster=False` 只是跳过签到，大厅还没开；先开大厅（这一步是主持人的动作）
    lobby_admin = await _admin_client()
    ready = await command(lobby_admin, match_id, "open_lobby")
    assert ready.status_code == 200, ready.text
    # 大厅已开；下面这个账号是**刚建的普通学生**，从未被加进任何名单
    plain = f"plain_{uuid.uuid4().hex[:6]}"
    await _mk_user(plain, ROLE_STUDENT, "tiger")
    c = make_client()
    await login(c, plain, "pass-123456")
    r = await c.post(f"/api/v1/matches/{match_id}/check-in")
    assert r.status_code == 200, r.text
    assert r.json()["house_key"] == "tiger"
    # 快照里也应当直接进入"已签到等待"流程，而不是被当成旁观者
    snap = await c.get(f"/api/v1/matches/{match_id}/snapshot")
    assert snap.status_code == 200, snap.text
    assert snap.json()["you"]["flow"] == "waiting", snap.text
    await c.aclose()


# ── 观战三档 ────────────────────────────────────────────────────────────────

async def test_spectator_modes_never_leak_answer(db_setup):
    """mirror 给题面；blur/summary 不给题面；**三档都绝不给标准答案**。"""
    match_id = await _mk_scoped_match(["tiger"])
    # 造一个进行中的回合
    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        m.status = "running"
        from app.db.models import MatchQuestion
        mq = (await db.execute(select(MatchQuestion).where(
            MatchQuestion.match_id == match_id))).scalars().first()
        r = RoundInstance(id=new_id(), match_id=match_id, match_question_id=mq.id,
                          attempt_no=1, status="open")
        db.add(r)
        await db.flush()
        m.current_round_id = r.id
        await db.commit()

    viewer = f"viewer_{uuid.uuid4().hex[:6]}"
    await _mk_user(viewer, ROLE_STUDENT, "tiger")
    c = make_client()
    await login(c, viewer, "pass-123456")

    for mode, want_prompt in (("mirror", True), ("blur", False), ("summary", False)):
        async with make_session_factory()() as db:
            m = await db.get(Match, match_id)
            m.spectator_mode = mode
            await db.commit()
        r = await c.get(f"/api/v1/matches/{match_id}/spectate/live")
        assert r.status_code == 200, r.text
        body = r.json()
        room = body.get("room") or {}
        has_prompt = "prompt" in room and bool(room.get("prompt"))
        assert has_prompt is want_prompt, f"{mode}: prompt={has_prompt} 期望 {want_prompt}"
        blob = r.text
        assert "correct_option_id" not in blob, f"{mode} 泄露了标准答案"
        assert "explanation" not in blob, f"{mode} 泄露了解析"
        assert body["match"]["spectator_mode"] == mode
    await c.aclose()


async def test_spectate_live_returns_submission_matrix(db_setup):
    """观战实时接口必须带提交矩阵（首页/大屏"活跃度"展示的数据源）。"""
    match_id = await _mk_scoped_match(["tiger"])
    viewer = f"m_{uuid.uuid4().hex[:6]}"
    await _mk_user(viewer, ROLE_STUDENT, "tiger")
    c = make_client()
    await login(c, viewer, "pass-123456")
    r = await c.get(f"/api/v1/matches/{match_id}/spectate/live")
    assert r.status_code == 200, r.text
    mx = r.json()["matrix"]
    assert set(mx.keys()) >= {"rounds", "houses", "max_rounds", "planned_questions"}
    for h in mx["houses"]:
        assert len(h["cells"]) == len(mx["rounds"]), "每院的格子数必须等于回合数"
    await c.aclose()


# ── 自助注册 / House 归属 / 封禁 ────────────────────────────────────────────

async def test_registration_closed_by_default(db_setup):
    anon = make_client()
    info = (await anon.get("/api/v1/auth/registration")).json()
    assert info["open"] is False, "默认必须关闭自助注册（避免上线当天被陌生人注册）"
    r = await anon.post("/api/v1/auth/register", json={
        "username": f"r_{uuid.uuid4().hex[:6]}", "password": "pass-12345678",
        "house_key": "tiger"})
    assert r.status_code == 403
    await anon.aclose()


async def test_registration_with_house_lock_and_no_self_change(db_setup, monkeypatch):
    """注册：House 必选、注册后锁定、本人无任何修改通路。"""
    admin = await _admin_client()
    r = await admin.patch("/api/v1/admin/settings", json={"registration_open": True})
    assert r.status_code == 200 and r.json()["registration_open"] is True

    anon = make_client()
    uname = f"selfreg_{uuid.uuid4().hex[:6]}"
    r = await anon.post("/api/v1/auth/register", json={
        "username": uname, "password": "pass-12345678", "house_key": "tortoise",
        "display_name": "小学员"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["house"]["key"] == "tortoise"
    assert body["user"]["house_locked"] is True
    assert body["user"]["self_registered"] is True
    assert "不能自行更改" in body["note"]
    # 注册即登录
    me = (await anon.get("/api/v1/auth/me")).json()
    assert me["username"] == uname and me["house"]["key"] == "tortoise"

    # 本人**没有**任何修改 House 的通路：尝试 PATCH 自己 → 404（该路由只对 admin 开放）
    r = await anon.patch(f"/api/v1/admin/users/{body['user']['id']}", json={"house_key": "tiger"})
    assert r.status_code in (401, 403), f"学生不应能调用管理端接口：{r.status_code}"
    # 归属未变
    me2 = (await anon.get("/api/v1/auth/me")).json()
    assert me2["house"]["key"] == "tortoise"

    # 管理员可以调整
    r = await admin.post(f"/api/v1/admin/users/{body['user']['id']}/house",
                         json={"house_key": "tiger", "reason": "转院"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["house"]["key"] == "tiger"

    await admin.patch("/api/v1/admin/settings", json={"registration_open": False})
    await anon.aclose()
    await admin.aclose()


async def test_registration_requires_valid_house(db_setup):
    admin = await _admin_client()
    await admin.patch("/api/v1/admin/settings", json={"registration_open": True})
    anon = make_client()
    r = await anon.post("/api/v1/auth/register", json={
        "username": f"bad_{uuid.uuid4().hex[:6]}", "password": "pass-12345678",
        "house_key": "nonexistent"})
    assert r.status_code == 422, r.text
    await admin.patch("/api/v1/admin/settings", json={"registration_open": False})
    await anon.aclose()
    await admin.aclose()


# ── 注册审核开关（免审 / 需审 两种流程）────────────────────────────────────

async def test_registration_review_flow(db_setup):
    """需审模式：注册后**不能登录**，管理员通过后才能用。

    关键点：待审账号不下发会话。若注册就发会话，审核开关形同虚设 ——
    这是"注册即登录"与"注册需审核"两档的本质差别。
    """
    admin = await _admin_client()
    await admin.patch("/api/v1/admin/settings",
                      json={"registration_open": True, "registration_review": True})
    info = (await admin.get("/api/v1/auth/registration")).json()
    assert info["needs_review"] is True, "注册页必须能提前知道需要审核"

    anon = make_client()
    uname = f"rev_{uuid.uuid4().hex[:6]}"
    r = await anon.post("/api/v1/auth/register", json={
        "username": uname, "password": "pass-12345678", "house_key": "dragon"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending_review"] is True, body
    assert body["user"]["status"] == "pending"
    assert "csrf_token" not in body, "待审账号不应拿到会话令牌"
    assert not anon.cookies.get("mgus_session"), "待审账号不应被下发会话 Cookie"

    # 待审期间不能登录，且错误码要能让学生看懂"是在等审核"
    r = await anon.post("/api/v1/auth/login",
                        json={"username": uname, "password": "pass-12345678"})
    assert r.status_code == 403 and r.json()["code"] == "ACCOUNT_PENDING", r.text

    # 出现在待审列表里，且带学院信息供管理员判断
    pend = (await admin.get("/api/v1/admin/users/pending")).json()
    mine = next((x for x in pend["items"] if x["username"] == uname), None)
    assert mine is not None, f"待审列表应包含 {uname}"
    assert mine["house"]["key"] == "dragon"
    assert mine["self_registered"] is True

    # 通过 → 可登录
    r = await admin.post(f"/api/v1/admin/users/{mine['id']}/review", json={"approve": True})
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    r = await login(anon, uname, "pass-12345678")
    assert r["user"]["house"]["key"] == "dragon"

    await admin.patch("/api/v1/admin/settings",
                      json={"registration_open": False, "registration_review": False})
    await anon.aclose()
    await admin.aclose()


async def test_registration_rejection_blocks_login(db_setup):
    """拒绝审核 → 账号停用且记原因；不能再登录。"""
    admin = await _admin_client()
    await admin.patch("/api/v1/admin/settings",
                      json={"registration_open": True, "registration_review": True})
    anon = make_client()
    uname = f"rej_{uuid.uuid4().hex[:6]}"
    await anon.post("/api/v1/auth/register", json={
        "username": uname, "password": "pass-12345678", "house_key": "phoenix"})
    pend = (await admin.get("/api/v1/admin/users/pending")).json()
    mine = next(x for x in pend["items"] if x["username"] == uname)

    r = await admin.post(f"/api/v1/admin/users/{mine['id']}/review",
                         json={"approve": False, "reason": "非本校学生"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    r = await anon.post("/api/v1/auth/login",
                        json={"username": uname, "password": "pass-12345678"})
    assert r.status_code == 403 and r.json()["code"] == "ACCOUNT_DISABLED", r.text

    # 已不在待审列表
    pend2 = (await admin.get("/api/v1/admin/users/pending")).json()
    assert all(x["username"] != uname for x in pend2["items"])

    await admin.patch("/api/v1/admin/settings",
                      json={"registration_open": False, "registration_review": False})
    await anon.aclose()
    await admin.aclose()


async def test_review_rejects_non_pending_account(db_setup):
    """对非待审账号调 review 必须被拒 —— 误操作不应静默改状态（封禁请用 /ban）。"""
    admin = await _admin_client()
    me = (await admin.get("/api/v1/auth/me")).json()
    r = await admin.post(f"/api/v1/admin/users/{me['id']}/review", json={"approve": True})
    assert r.status_code == 409, r.text
    await admin.aclose()


async def test_auto_approve_registration_is_immediately_usable(db_setup):
    """免审模式（默认）：注册后立即可用、直接拿到会话，不需要管理员介入。"""
    admin = await _admin_client()
    await admin.patch("/api/v1/admin/settings",
                      json={"registration_open": True, "registration_review": False})
    anon = make_client()
    uname = f"auto_{uuid.uuid4().hex[:6]}"
    r = await anon.post("/api/v1/auth/register", json={
        "username": uname, "password": "pass-12345678", "house_key": "tortoise"})
    assert r.status_code == 200, r.text
    assert r.json()["pending_review"] is False
    assert anon.cookies.get("mgus_session"), "免审注册应直接登录"
    me = (await anon.get("/api/v1/auth/me")).json()
    assert me["username"] == uname and me["status"] == "active"
    await admin.patch("/api/v1/admin/settings", json={"registration_open": False})
    await anon.aclose()
    await admin.aclose()


async def test_ban_kicks_session_immediately(db_setup):
    """封禁必须**立即**踢下线：只改 status 不够，已登录设备要能继续答题就白封了。"""
    uname = f"banme_{uuid.uuid4().hex[:6]}"
    uid = await _mk_user(uname, ROLE_STUDENT, "tiger")
    c = make_client()
    await login(c, uname, "pass-123456")
    assert (await c.get("/api/v1/auth/me")).status_code == 200

    admin = await _admin_client()
    r = await admin.post(f"/api/v1/admin/users/{uid}/ban",
                         json={"banned": True, "reason": "测试封禁"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"

    # 会话被吊销 → 立即失效
    r = await c.get("/api/v1/auth/me")
    assert r.status_code == 401, f"封禁后旧会话仍可用：{r.text}"
    # 重新登录也被拒
    r = await c.post("/api/v1/auth/login", json={"username": uname, "password": "pass-123456"})
    assert r.status_code == 403 and r.json()["code"] == "ACCOUNT_DISABLED"

    # 解封后可以恢复
    r = await admin.post(f"/api/v1/admin/users/{uid}/ban", json={"banned": False})
    assert r.status_code == 200 and r.json()["status"] == "active"
    await c.aclose()
    await admin.aclose()


async def test_cannot_ban_admin(db_setup):
    admin = await _admin_client()
    me = (await admin.get("/api/v1/auth/me")).json()
    r = await admin.post(f"/api/v1/admin/users/{me['id']}/ban", json={"banned": True})
    assert r.status_code == 422, "不得封禁管理员（否则可能把自己锁在门外）"
    await admin.aclose()


async def test_admin_overview_counts_match_reality(db_setup):
    """数据大屏的人数必须与库里一致，并给出可上场的 active 人数。"""
    admin = await _admin_client()
    r = await admin.get("/api/v1/admin/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) >= {"houses", "unassigned", "totals", "settings"}
    factory = make_session_factory()
    async with factory() as db:
        real = (await db.execute(select(User))).scalars().all()
        for hid_row in body["houses"]:
            h = (await db.execute(select(House).where(House.key == hid_row["house_key"]))).scalar_one()
            mine = [u for u in real if u.house_id == h.id]
            assert hid_row["members"] == len(mine), f"{hid_row['house_key']} 人数不符"
            assert hid_row["active_members"] == len([u for u in mine if u.status == "active"])
            assert hid_row["banned"] == len([u for u in mine if u.status != "active"])
    assert body["totals"]["users"] == len(real)
    await admin.aclose()
