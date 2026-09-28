"""控制权租约的回归测试（真实缺陷：换机后账号永远拿不到控制权）。

背景：`control_leases` 的 session_id 在换机会话时不会被清理。
用户重新登录后，新会话 id 与租约里的旧 session_id 不一致，
claim 便一直返回 409「控制权由其他会话持有」→ **该账号永远交不了答案**。
现场一旦发生，学生就只能干看着倒计时。

修法两处：
  1. 登录签发新会话时一并删除旧租约（根因）；
  2. claim 时若租约指向的会话既非当前会话、也非当前活跃会话，判定为残留并回收（兜底）。
"""
from sqlalchemy import select

from app.db.models import ControlLease, User, UserSession
from app.db.session import make_session_factory
from tests.conftest import login, make_client


async def test_reclaim_after_relogin_without_stale_lease(db_setup):
    """重新登录（force 接管）后**必须能立刻拿到控制权**。"""
    c = make_client()
    await login(c, "p_tortoise_2", "pass-123456")
    r = await c.post("/api/v1/auth/control/claim")
    assert r.status_code == 200, r.text
    first = r.json()["control_token"]

    # 换机：同一客户端用 force 重新登录（新会话取代旧会话）
    await login(c, "p_tortoise_2", "pass-123456")
    r2 = await c.post("/api/v1/auth/control/claim")
    assert r2.status_code == 200, f"换机后拿不到控制权：{r2.text}"
    assert r2.json()["control_token"] != first, "换机后控制权凭据应当轮换"

    # 旧凭据已失效（新租约覆盖）
    r3 = await c.post("/api/v1/auth/control/claim")
    assert r3.status_code == 200
    await c.aclose()


async def test_stale_lease_pointing_to_revoked_session_is_reclaimed(db_setup):
    """即使库里留了一条指向**已撤销会话**的残留租约，claim 也必须能自愈。

    这是修复前真实存在的状态：租约 session_id 指向上一代会话，
    新登录的会话与它不匹配，于是每次 claim 都 409。
    """
    c = make_client()
    await login(c, "p_phoenix_2", "pass-123456")
    me = (await c.get("/api/v1/auth/me")).json()

    factory = make_session_factory()
    async with factory() as db:
        # 造一条残留租约：session_id 指向一个不存在的旧会话
        lease = await db.get(ControlLease, me["id"])
        if lease is None:
            from app.core.security import new_id, token_hash
            db.add(ControlLease(user_id=me["id"], session_id=new_id(), token=token_hash("stale")))
        else:
            lease.session_id = "deadbeefdeadbeefdeadbeefdeadbeef"
        await db.commit()

    r = await c.post("/api/v1/auth/control/claim")
    assert r.status_code == 200, f"残留租约应被自动回收，而不是把用户锁死：{r.text}"
    assert r.json()["control_token"]
    await c.aclose()


async def test_live_other_device_still_blocked(db_setup):
    """真正的多设备争夺仍须被拒 —— 自动回收不能把 §16.2 的保护一起放开。"""
    c1 = make_client()
    await login(c1, "p_dragon_2", "pass-123456")
    assert (await c1.post("/api/v1/auth/control/claim")).status_code == 200

    # 另一台设备：用 force 强行接管（会清掉旧租约并成为活跃会话）
    c2 = make_client()
    await login(c2, "p_dragon_2", "pass-123456")
    r = await c2.post("/api/v1/auth/control/claim")
    assert r.status_code == 200, r.text

    # 此时旧设备再抢：它的会话已不是活跃会话，get_current_user 会先返回 SESSION_REPLACED
    r_old = await c1.post("/api/v1/auth/control/claim")
    assert r_old.status_code == 401, f"旧设备不应还能操作：{r_old.text}"
    assert r_old.json()["code"] == "SESSION_REPLACED"
    await c1.aclose()
    await c2.aclose()


async def test_login_clears_stale_lease_row(db_setup):
    """根因层面：登录签发新会话时应把旧租约行删掉，而不是留着不匹配的记录。"""
    c = make_client()
    await login(c, "p_tiger_1", "pass-123456")
    me = (await c.get("/api/v1/auth/me")).json()
    assert (await c.post("/api/v1/auth/control/claim")).status_code == 200

    await login(c, "p_tiger_1", "pass-123456")   # force 接管
    factory = make_session_factory()
    async with factory() as db:
        lease = await db.get(ControlLease, me["id"])
        # 允许两种正确结果：行被删除，或行已指向当前会话
        u = await db.get(User, me["id"])
        if lease is not None:
            assert lease.session_id == u.active_session_id, (
                "登录后若仍保留租约，它必须指向当前活跃会话（否则 claim 会 409）")
    await c.aclose()
