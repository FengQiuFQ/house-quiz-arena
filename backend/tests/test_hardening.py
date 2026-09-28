"""B1 地基层验收：CSRF、登录限流、强制改密轮换、单写者守卫、回合关闭不阻塞、进度聚合。

这些用例对应的都是"上线才会炸"的问题，逐条对应 docs/production-readiness-audit.md 的编号。
"""
import os
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.ratelimit import clear_login_failures
from app.core.security import hash_password
from app.db.models import User, ROLE_STUDENT
from app.db.session import make_session_factory
from app.main import app
from app.core.security import new_id
from tests.conftest import login, make_client


# ── B1-E5 CSRF ──────────────────────────────────────────────────────────────

async def test_csrf_missing_header_is_rejected(client, db_setup):
    """带会话 Cookie 但没有 X-CSRF-Token 的写操作必须被拒（403 CSRF_FAILED）。"""
    await login(client, "p_tiger_1", "pass-123456")
    assert client.cookies.get("mgus_session")
    # 裸客户端：不复用事件钩子，因此不会自动注入 CSRF 头
    raw = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    raw.cookies.set("mgus_session", client.cookies.get("mgus_session"))
    r = await raw.post("/api/v1/auth/control/claim")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "CSRF_FAILED"
    await raw.aclose()


async def test_csrf_wrong_token_is_rejected(client, db_setup):
    await login(client, "p_tiger_1", "pass-123456")
    raw = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    raw.cookies.set("mgus_session", client.cookies.get("mgus_session"))
    r = await raw.post("/api/v1/auth/control/claim", headers={"X-CSRF-Token": "forged-token"})
    assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"
    await raw.aclose()


async def test_csrf_correct_token_passes(client, db_setup):
    await login(client, "p_tiger_1", "pass-123456")
    r = await client.post("/api/v1/auth/control/claim")
    assert r.status_code == 200, r.text
    assert r.json()["control_token"]


async def test_login_sets_readable_csrf_cookie(client, db_setup):
    body = await login(client, "p_dragon_1", "pass-123456")
    # 非 HttpOnly：前端要能读出来放进请求头
    assert client.cookies.get("mgus_csrf")
    assert client.cookies.get("mgus_csrf") == client.cookies.get("mgus_session")
    assert body["csrf_token"] == client.cookies.get("mgus_session")


# ── B1-E6 登录限流（此前该配置项从未被任何代码调用）─────────────────────────

async def test_login_failure_rate_limit(client, db_setup, monkeypatch):
    """连续失败必须触发 429（配置在 conftest 里被整体放宽，这里单独调回真实值）。"""
    from app.core.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_login_fail_per_min", 5)
    username = f"nobody_{uuid.uuid4().hex[:8]}"
    clear_login_failures(username)
    seen_429 = False
    for _ in range(12):
        r = await client.post("/api/v1/auth/login",
                              json={"username": username, "password": "definitely-wrong"})
        if r.status_code == 429:
            seen_429 = True
            assert r.json()["code"] == "RATE_LIMITED"
            assert r.json()["details"].get("retry_after_s", 0) > 0
            break
        assert r.status_code == 401
    assert seen_429, "连续失败登录必须触发 429，否则口令可被无限爆破"
    clear_login_failures(username)


async def test_successful_login_clears_failure_counter(client, db_setup):
    """正常用户打错一次不应被锁住：成功即清零。"""
    clear_login_failures("p_phoenix_1")
    r = await client.post("/api/v1/auth/login",
                          json={"username": "p_phoenix_1", "password": "oops"})
    assert r.status_code == 401
    body = await login(client, "p_phoenix_1", "pass-123456")
    assert body["session_generation"] >= 1
    # 再打错一次仍应是 401 而不是被累计锁死
    r = await client.post("/api/v1/auth/login",
                          json={"username": "p_phoenix_1", "password": "oops"})
    assert r.status_code == 401


# ── B1-E7 强制改密 + 令牌轮换 ───────────────────────────────────────────────

async def test_must_change_password_blocks_business_endpoints(client, db_setup):
    username = f"fresh_{uuid.uuid4().hex[:8]}"
    factory = make_session_factory()
    async with factory() as db:
        db.add(User(id=new_id(), username=username,
                    password_hash=hash_password("initial-pass-1"),
                    role=ROLE_STUDENT, must_change_password=True))
        await db.commit()

    body = await login(client, username, "initial-pass-1")
    assert body["user"]["must_change_password"] is True

    # 业务接口被拒（练习已支持匿名，因此用必须登录的控制权认领接口断言）
    r = await client.post("/api/v1/auth/control/claim")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    # 白名单接口仍可用
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 200

    old_gen = body["session_generation"]
    new_csrf = client.cookies.get("mgus_csrf")
    r = await client.post("/api/v1/auth/change-password",
                          json={"old_password": "initial-pass-1", "new_password": "brand-new-pass-9"})
    assert r.status_code == 200, r.text
    # 令牌必须轮换：否则旧设备残留页面仍能用旧 Cookie 操作
    assert r.json()["session_generation"] == old_gen + 1
    assert client.cookies.get("mgus_session") != new_csrf

    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 200 and r.json()["must_change_password"] is False


async def test_change_password_rejects_same_password(client, db_setup):
    await login(client, "p_tortoise_1", "pass-123456")
    r = await client.post("/api/v1/auth/change-password",
                          json={"old_password": "pass-123456", "new_password": "pass-123456"})
    assert r.status_code == 422


# ── B1-E4 单写者守卫 ────────────────────────────────────────────────────────

def test_single_writer_guard_rejects_multiple_workers(monkeypatch):
    from app.main import _assert_single_writer
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with pytest.raises(RuntimeError) as ei:
        _assert_single_writer()
    assert "只支持单进程" in str(ei.value)
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    _assert_single_writer()   # 不抛


# ── B1-E3 广播不派生任务 ────────────────────────────────────────────────────

async def test_broadcast_does_not_spawn_tasks():
    """每条消息每连接一个 Task 会在 200 人时变成任务风暴；这里锁死为同步入队。"""
    import asyncio
    from app.realtime.hub import Conn, Hub

    class DummyWs:
        pass

    hub = Hub()
    for i in range(50):
        hub.conns.add(Conn(ws=DummyWs(), user_id=f"u{i}", role="entrant", match_id="m1"))
    before = len(asyncio.all_tasks())
    hub.broadcast("m1", {"type": "progress.snapshot", "payload": {}})
    after = len(asyncio.all_tasks())
    assert after == before, "广播必须同步入队，不得为每个连接创建 Task"
    assert all(c.queue.qsize() == 1 for c in hub.conns)


# ── B1-E2 回合关闭不得阻塞 ──────────────────────────────────────────────────

async def test_close_round_never_sleeps(db_setup):
    """在途计数 > 0 时关闭回合也必须立刻返回（原实现最长 sleep 3 秒）。"""
    from app.db.models import (
        Match, MatchQuestion, Question, QuestionVersion, RoundInstance,
        MATCH_RUNNING, ROUND_DISPLAYING,
    )
    from app.modules.competitions.service import close_round, inflight_begin, inflight_end
    from app.db.base import utcnow

    factory = make_session_factory()
    async with factory() as db:
        q = Question(id=new_id(), subject="科学常识", difficulty=1, language="zh",
                     pool="official", status="published", prompt_hash="h-close")
        v = QuestionVersion(id=new_id(), question_id=q.id, version_no=1, prompt="?",
                            options_json=[{"option_id": "o1", "text": "a"}],
                            correct_option_id="o1", review_state="approved")
        db.add_all([q, v])
        q.current_version_id = v.id
        m = Match(id=new_id(), title="[DEMO] close-round", status=MATCH_RUNNING, state_version=1)
        db.add(m)
        await db.flush()
        mq = MatchQuestion(id=new_id(), match_id=m.id, question_id=q.id,
                           version_id=v.id, position=1)
        db.add(mq)
        await db.flush()
        r = RoundInstance(id=new_id(), match_id=m.id, match_question_id=mq.id,
                          attempt_no=1, status=ROUND_DISPLAYING,
                          open_at=(utcnow().replace(tzinfo=None)), deadline_at=utcnow().replace(tzinfo=None))
        db.add(r)
        await db.flush()

        inflight_begin(r.id)
        t0 = time.monotonic()
        await close_round(db, m, r, utcnow())
        elapsed = time.monotonic() - t0
        inflight_end(r.id)
        assert elapsed < 0.2, f"close_round 阻塞了 {elapsed:.2f}s，会拖慢同一事务内其他比赛"
        assert r.status == "closed"
        await db.commit()
