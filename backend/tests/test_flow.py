"""端到端集成：开赛→同步回合→判分→平分并列→锁定→揭晓保护→发布→首页（§9.2 垂直切片）。

每个选手使用独立 httpx 客户端（等价于每人一台设备/浏览器，符合单活跃会话模型）。
控制器时间经 advance(now=…) 注入，提交路径仍走真实 HTTP 与真实接收时间。
"""
import uuid
from datetime import timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db.base import utcnow
from sqlalchemy import select as _sel
from app.db.models import Match
from app.db.session import make_session_factory
from app.main import app
from tests.conftest import command, login, make_match, advance_factory, make_client


async def _user_client(username: str, password: str) -> AsyncClient:
    c = make_client()
    await login(c, username, password)
    return c


async def _drive_round(match_id, admin: AsyncClient, players: dict, adv,
                       control_tokens: dict[str, str],
                       answers: dict[str, str | None]) -> str:
    """推进：创建回合 → 库内强制开放（窗口锚定真实时钟）→ 提交 → 收缩截止 → 关闭。"""
    from app.db.models import RoundInstance as RI
    from app.db.session import make_session_factory
    await adv(match_id, utcnow())                      # 控制器创建新回合（展示期）
    now = utcnow()
    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        rr = await db.get(RI, m.current_round_id)
        rr.status = "open"
        rr.display_at = (now - timedelta(seconds=2)).replace(tzinfo=None)
        rr.open_at = (now - timedelta(seconds=1)).replace(tzinfo=None)
        rr.deadline_at = (now + timedelta(minutes=5)).replace(tzinfo=None)
        await db.commit()
        rid = rr.id
    snap = (await admin.get(f"/api/v1/matches/{match_id}/snapshot")).json()
    rd = snap["round"]
    assert rd and rd["phase"] == "open", f"回合应已开放: {snap}"
    rid = rd["round_instance_id"]
    for username, opt in answers.items():
        if opt is None:
            continue
        pc = players[username]
        r = await pc.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                          json={"option_id": opt},
                          headers={"Idempotency-Key": f"k-{uuid.uuid4().hex[:12]}",
                                   "X-Control-Token": control_tokens[username]})
        assert r.status_code == 200, f"{username}: {r.text}"
    async with factory() as db:
        rr = await db.get(RI, rid)
        rr.deadline_at = (utcnow() - timedelta(seconds=1)).replace(tzinfo=None)
        await db.commit()
    await adv(match_id, utcnow())                      # 关闭（closed_at=真实当前时刻）
    return rid


async def test_full_match_flow(client, db_setup):
    match_id = await make_match(db_setup, client, question_count=2, fixed_k=1)
    adv = advance_factory()
    admin = await _user_client("admin", "admin-pass-123")

    # 大厅已由 make_match 开启 → 直接签到
    players = {}
    for u in ("p_tiger_1", "p_dragon_1", "p_tortoise_1", "p_phoenix_1"):
        players[u] = await _user_client(u, "pass-123456")
        r = await players[u].post(f"/api/v1/matches/{match_id}/check-in")
        assert r.status_code == 200, r.text

    # 没有绑定学院的账号不能签到 —— 管理员也一样：
    # 参赛资格已改为"按学院"，角色不再决定能否上场（旧的两道门：entrant 角色 + 单独加资格）。
    # 复用**同一个** admin 客户端：另开一个会顶掉它的会话（单账号单活跃会话）。
    r = await admin.post(f"/api/v1/matches/{match_id}/check-in")
    assert r.status_code == 403 and r.json()["code"] == "HOUSE_REQUIRED", r.text

    # 开赛
    r = await command(admin, match_id, "start_match")
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text

    # 每人认领答题控制权（进入比赛页的常规动作）
    control_tokens: dict[str, str] = {}
    for name, pc in players.items():
        r = await pc.post("/api/v1/auth/control/claim")
        assert r.status_code == 200, r.text
        control_tokens[name] = r.json()["control_token"]

    # 回合 1：四人全对 → 四 House 各 100
    await _drive_round(match_id, admin, players, adv, control_tokens,
                       {"p_tiger_1": "o1", "p_dragon_1": "o1", "p_tortoise_1": "o1",
                        "p_phoenix_1": "o1"})
    # 回合 2：tiger/dragon 对、tortoise 错、phoenix 不答（超时视为未答）
    await _drive_round(match_id, admin, players, adv, control_tokens,
                       {"p_tiger_1": "o1", "p_dragon_1": "o1", "p_tortoise_1": "o2",
                        "p_phoenix_1": None})

    # 题目耗尽 → 自动锁定
    await adv(match_id, utcnow() + timedelta(minutes=10))
    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        assert m.status == "locked", m.status

    # 未发布前公开结果被拒
    r = await admin.get(f"/api/v1/matches/{match_id}/results")
    assert r.status_code == 403 and r.json()["code"] == "RESULT_NOT_PUBLIC"
    # 匿名同样被拒
    anon = make_client()
    r = await anon.get(f"/api/v1/matches/{match_id}/results")
    assert r.status_code == 403

    # 结算（tiger/dragon 100 并列第 1；tortoise/phoenix 各 50 并列第 3）
    r = await command(admin, match_id, "calculate")
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    pv = (await admin.get(f"/api/v1/admin/matches/{match_id}/preview-results")).json()
    assert pv["entry_point"] == "preview" and pv["published"] is False
    hs = {h["key"]: h for h in pv["houses"]}
    assert hs["tiger"]["score"] == 100.0 and hs["dragon"]["score"] == 100.0
    assert hs["tiger"]["rank"] == 1 and hs["tiger"]["is_tie"]
    assert hs["dragon"]["rank"] == 1 and hs["dragon"]["is_tie"]
    # 未答题计零、不缩分母：phoenix 第 2 题未答仍按 2 题分母计 50，与 tortoise 并列
    assert hs["tortoise"]["score"] == 50.0 and hs["tortoise"]["rank"] == 3 and hs["tortoise"]["is_tie"]
    assert hs["phoenix"]["score"] == 50.0 and hs["phoenix"]["rank"] == 3 and hs["phoenix"]["is_tie"]

    # 揭晓保护：已安排、未到点仍不可取
    now = utcnow()
    r = await command(admin, match_id, "schedule_reveal", payload={
        "ceremony_start_at": (now + timedelta(seconds=2)).isoformat() + "Z",
        "reveal_at": (now + timedelta(seconds=3)).isoformat() + "Z"})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    r = await admin.get(f"/api/v1/matches/{match_id}/results")
    assert r.status_code == 403 and r.json()["code"] == "RESULT_NOT_PUBLIC"

    # 到点发布（控制器 tick 逻辑，含重启后的同路径）
    await adv(match_id, now + timedelta(seconds=10))
    r = await admin.get(f"/api/v1/matches/{match_id}/results")
    assert r.status_code == 200, r.text
    pub = r.json()
    assert pub["entry_point"] == "published"
    assert pub["is_demo"] is True  # [DEMO] 标题 → 演示标记
    assert {h["key"] for h in pub["houses"] if h["rank"] == 1} == {"tiger", "dragon"}

    # 首页出现已发布数据
    r = await admin.get("/api/v1/home")
    assert r.status_code == 200
    home = r.json()
    assert home["has_published"] is True and home["demo"] is False
    tiger = next(h for h in home["houses"] if h["key"] == "tiger")
    assert tiger["latest"]["score"] == 100.0

    for pc in players.values():
        await pc.aclose()
    await admin.aclose()
    await anon.aclose()


async def test_command_idempotency_and_version_conflict(client, db_setup):
    # with_roster=False：停在 draft，由本测试自行驱动 open_lobby
    match_id = await make_match(db_setup, client, question_count=2, fixed_k=1,
                                with_roster=False)
    admin = await _user_client("admin", "admin-pass-123")
    r = await admin.get("/api/v1/admin/matches")
    sv = next(m["state_version"] for m in r.json()["items"] if m["id"] == match_id)

    cid = f"cmd-{uuid.uuid4().hex[:12]}"
    r1 = await command(admin, match_id, "open_lobby", command_id=cid)
    assert r1.status_code == 200 and r1.json()["status"] == "ok"
    r2 = await command(admin, match_id, "open_lobby", command_id=cid)
    assert r2.status_code == 200
    assert r2.json()["status"] == "replayed", "重复命令必须回放原结果（§10.2）"

    # 过期版本 → 冲突拒绝，命令留痕
    r = await admin.get("/api/v1/admin/matches")
    cur = next(m["state_version"] for m in r.json()["items"] if m["id"] == match_id)
    r = await admin.post(f"/api/v1/admin/matches/{match_id}/commands", json={
        "command_id": f"cmd-{uuid.uuid4().hex[:12]}", "action": "open_lobby",
        "expected_state_version": cur - 1, "payload": {}})
    assert r.status_code == 409
    assert r.json()["code"] == "STATE_VERSION_CONFLICT"
    r = await admin.get(f"/api/v1/admin/matches/{match_id}/commands")
    assert any(c["error_code"] == "STATE_VERSION_CONFLICT" for c in r.json()["items"])

    # 非法跳转：lobby → locked
    r = await admin.get("/api/v1/admin/matches")
    sv = next(m["state_version"] for m in r.json()["items"] if m["id"] == match_id)
    r = await admin.post(f"/api/v1/admin/matches/{match_id}/commands", json={
        "command_id": f"cmd-{uuid.uuid4().hex[:12]}", "action": "lock",
        "expected_state_version": sv, "payload": {}})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION"
    await admin.aclose()


async def test_anonymous_cannot_see_pending_data(client, db_setup):
    """匿名快照不含题目答案与个人字段（§8、§10.4）。"""
    match_id = await make_match(db_setup, client, question_count=2, fixed_k=1)
    admin = await _user_client("admin", "admin-pass-123")
    await command(admin, match_id, "open_lobby")
    players = {}
    for u in ("p_tiger_1", "p_dragon_1", "p_tortoise_1", "p_phoenix_1"):
        players[u] = await _user_client(u, "pass-123456")
        await players[u].post(f"/api/v1/matches/{match_id}/check-in")
    await command(admin, match_id, "start_match")
    adv = advance_factory()
    await adv(match_id, utcnow() - timedelta(seconds=5))    # 回溯创建回合
    await adv(match_id, utcnow())                           # 进入作答期

    anon = make_client()
    r = await anon.get(f"/api/v1/matches/{match_id}/snapshot")
    # 规则变更（产品红线）：竞赛相关一律需要登录，未登录直接 401。
    # 原断言是 200 + role=anonymous；现在连聚合进度都不给匿名——
    # 因为观战要按 House 裁剪，没有身份就无从判断该看哪个学院。
    assert r.status_code == 401, r.text
    assert r.json()["code"] == "LOGIN_REQUIRED"
    blob = r.text
    assert "correct_option_id" not in blob, "匿名响应不得包含正确答案"
    assert "explanation" not in blob
    # 观战接口分层：匿名可看公开总览，但依然不给题目与答案
    r2 = await anon.get(f"/api/v1/matches/{match_id}/spectate")
    assert r2.status_code == 200, r2.text
    assert r2.json()["viewer"]["role"] == "anonymous"
    assert "correct_option_id" not in r2.text and "explanation" not in r2.text
    r3 = await anon.get(f"/api/v1/matches/{match_id}/spectate/live")
    assert r3.status_code == 200, r3.text
    assert "correct_option_id" not in r3.text and "explanation" not in r3.text
    await anon.aclose()
    for pc in players.values():
        await pc.aclose()
    await admin.aclose()
