"""会话与换机（§16）：单活跃会话、force 接管、交接码、控制权租约。

换机规则的**产品口径**（2026-09 修订，此前实现过严）：
  只有"选手**正在答题界面** + 原设备仍在活跃"才要求一次性交接码。
  管理员换机、签到阶段（lobby）、题间过渡、已交卷、原设备静默超过
  `handover_idle_seconds`（默认 60s）—— 这几种情况一律直接放行。
  理由：单活跃会话的目的只是**防止同一名选手在两台设备同时作答**，
  不是"一个账号只能在一台设备上登录"。
"""
from datetime import timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import User, UserSession
from app.db.session import make_session_factory
from app.main import app
from tests.conftest import advance_factory, command, login, make_client, make_match


async def test_login_and_me(client, db_setup):
    body = await login(client, "p_tiger_2", "pass-123456")
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 200
    me = r.json()
    assert me["username"] == "p_tiger_2"
    assert me["house"]["key"] == "tiger"
    assert me["session_generation"] == body["session_generation"]


async def test_wrong_password(client, db_setup):
    r = await client.post("/api/v1/auth/login",
                          json={"username": "p_tiger_2", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


async def test_single_active_session_and_force(client, db_setup):
    body = await login(client, "p_dragon_2", "pass-123456")
    gen0 = body["session_generation"]
    old_cookie = dict(client.cookies)

    # 不带 force 的第二台设备登录：
    # 该账号**没有在答题**（不在任何比赛名单里），因此服务端直接放行并取代旧会话。
    # 这条以前断言 409 —— 那是错的：把"有活跃会话"一律当成需要交接，
    # 会让现场出现"我明明没在答题，换台设备还要掏交接码"（管理员换机时尤其荒谬）。
    r = await client.post("/api/v1/auth/login",
                          json={"username": "p_dragon_2", "password": "pass-123456"})
    assert r.status_code == 200, r.text
    assert r.json()["session_generation"] == gen0 + 1

    # 旧设备的令牌立即失效（代次已推进）
    client.cookies.clear()
    for k, v in old_cookie.items():
        client.cookies.set(k, v)
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["code"] == "SESSION_REPLACED"


async def test_handover_required_only_while_answering(client, db_setup):
    """换机规则的四种情形（产品口径：只有"正在答题界面"才要交接）。

    这是现场最容易做错的一处：判早了学生换不了设备，判晚了就能一人两屏。
    """
    factory = make_session_factory()
    idle_s = get_settings().handover_idle_seconds
    match_id = await make_match(db_setup, client, question_count=1, display=0, answer=120)
    r = await command(client, match_id, "start_match")
    assert r.status_code == 200, r.text
    await advance_factory()(match_id, utcnow())      # 开题（抢答：回合直接 open）

    # ① 管理员换机：永远不需要交接（后台/投屏来回切是日常工作）
    pc = make_client()
    try:
        await login(pc, "admin", "admin-pass-123")
        r2 = await pc.post("/api/v1/auth/login",
                           json={"username": "admin", "password": "admin-pass-123"})
        assert r2.status_code == 200, f"管理员换机不应被要求交接：{r2.status_code} {r2.text}"
    finally:
        await pc.aclose()

    # ② 选手正在答题 + 原会话仍活跃 → 必须交接（防一人两屏的唯一场景）
    a = make_client()
    try:
        await login(a, "p_dragon_1", "pass-123456")
        assert (await a.get(f"/api/v1/matches/{match_id}/snapshot")).status_code == 200

        b = make_client()
        try:
            r2 = await b.post("/api/v1/auth/login",
                              json={"username": "p_dragon_1", "password": "pass-123456"})
            assert r2.status_code == 409, f"正在答题时换设备应要求交接：{r2.status_code}"
            assert r2.json()["code"] == "SESSION_ACTIVE"
            assert r2.json()["details"]["needs_transfer"] is True
            assert r2.json()["details"]["reason"] == "answering"
        finally:
            await b.aclose()

        # ③ 原会话静默超过阈值（关掉标签页/锁屏走人）→ 直接放行，不再要交接码
        async with factory() as db:
            u = (await db.execute(select(User).where(
                User.username == "p_dragon_1"))).scalar_one()
            sess = await db.get(UserSession, u.active_session_id)
            sess.last_seen_at = utcnow() - timedelta(seconds=idle_s + 1)
            await db.commit()
        c = make_client()
        try:
            r3 = await c.post("/api/v1/auth/login",
                              json={"username": "p_dragon_1", "password": "pass-123456"})
            assert r3.status_code == 200, \
                f"静默超过 {idle_s}s 后换设备应放行：{r3.status_code} {r3.text}"
        finally:
            await c.aclose()
    finally:
        await a.aclose()


async def test_lobby_phase_does_not_require_handover(client, db_setup):
    """签到阶段（lobby）换设备不需要交接：那时还没有任何可作答的题。

    原来把 lobby 也算"比赛中"，于是学生签完到就再也换不了设备 ——
    现场表现是"还没开始答题就要我掏交接码"。
    """
    await make_match(db_setup, client, question_count=1, with_roster=True)  # 停在 lobby

    # 设备甲：用选手账号登录（此时比赛在 lobby，未开赛）
    a = make_client()
    try:
        await login(a, "p_phoenix_1", "pass-123456")
        assert (await a.get("/api/v1/auth/me")).status_code == 200

        # 设备乙：同一账号再次登录。**必须用独立 client**，
        # 否则 cookie 罐共用，等于在同一台设备上重复登录，构造不出"换机"。
        #
        # 注意：本测试必须**自己造一个干净场景**再验。
        # 会话是按账号全局的，而别的用例可能已经把这个账号放进了"进行中且已开题"的比赛
        # （实测踩到：跨用例污染让本用例拿到 reason=answering）。
        # 因此这里先把这个选手从其它进行中的比赛里摘出去，只留下 lobby 这一场。
        factory = make_session_factory()
        async with factory() as db:
            from app.db.models import Match, MatchEntry, MATCH_PAUSED, MATCH_RUNNING
            u = (await db.execute(select(User).where(
                User.username == "p_phoenix_1"))).scalar_one()
            live_ids = (await db.execute(select(Match.id).where(
                Match.status.in_([MATCH_RUNNING, MATCH_PAUSED])))).scalars().all()
            if live_ids:
                for e in (await db.execute(select(MatchEntry).where(
                        MatchEntry.user_id == u.id,
                        MatchEntry.match_id.in_(live_ids)))).scalars().all():
                    await db.delete(e)
                await db.commit()

        b = make_client()
        try:
            r2 = await b.post("/api/v1/auth/login",
                              json={"username": "p_phoenix_1", "password": "pass-123456"})
            assert r2.status_code == 200, f"签到阶段换设备应放行：{r2.status_code} {r2.text}"
        finally:
            await b.aclose()

        # 设备甲随即失效（新会话取代了它）——"放行"不等于"两台都能用"
        assert (await a.get("/api/v1/auth/me")).status_code == 401
    finally:
        await a.aclose()


async def test_transfer_flow(client, db_setup):
    body = await login(client, "p_tortoise_2", "pass-123456")
    gen0 = body["session_generation"]
    r = await client.post("/api/v1/auth/transfer")
    assert r.status_code == 200
    code = r.json()["transfer_code"]

    # 交接码登录（比赛中换机的合法路径），代次继续 +1
    r = await client.post("/api/v1/auth/login",
                          json={"username": "p_tortoise_2", "password": "pass-123456",
                                "transfer_code": code})
    assert r.status_code == 200
    assert r.json()["session_generation"] == gen0 + 1

    # 交接码一次性：复用失败
    r = await client.post("/api/v1/auth/login",
                          json={"username": "p_tortoise_2", "password": "pass-123456",
                                "transfer_code": code})
    assert r.status_code == 400


async def test_control_lease(client, db_setup):
    await login(client, "p_phoenix_2", "pass-123456")
    r = await client.post("/api/v1/auth/control/claim")
    assert r.status_code == 200
    assert r.json()["control_token"]

    # 同会话再认领成功（幂等续期）
    r = await client.post("/api/v1/auth/control/claim")
    assert r.status_code == 200

    # 匿名拿不到控制权
    client.cookies.clear()
    r = await client.post("/api/v1/auth/control/claim")
    assert r.status_code == 401
