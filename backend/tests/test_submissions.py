"""答案提交：幂等、截止、控制权、资格（§10.2、§10.6 对应场景）。"""
import uuid
from datetime import timedelta

from app.core.security import new_id
from app.db.base import as_utc, utcnow
from app.db.models import Match, MatchEntry, RoundInstance, ROUND_OPEN
from app.db.session import make_session_factory
from tests.conftest import command, login, make_match


async def _setup_open_round(db_setup, client, match_id: str, deadline_s: float = 60):
    """开赛并把第一回合置于开放窗口（make_match 已完成 lobby+签到）。"""
    r = await command(client, match_id, "start_match")
    assert r.status_code == 200, r.text
    # 控制器逻辑推进一次：产生第一回合（展示期）
    from tests.conftest import advance_factory
    await advance_factory()(match_id, utcnow())
    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        assert m.current_round_id, "应已产生第一回合"
        r_ = await db.get(RoundInstance, m.current_round_id)
        now = utcnow()
        r_.status = ROUND_OPEN
        r_.display_at = (now - timedelta(seconds=5)).replace(tzinfo=None)
        r_.open_at = (now - timedelta(seconds=1)).replace(tzinfo=None)
        r_.deadline_at = (now + timedelta(seconds=deadline_s)).replace(tzinfo=None)
        await db.commit()
        return r_.id


async def _entry_id(db_setup, username: str, match_id: str) -> str:
    factory = make_session_factory()
    async with factory() as db:
        from sqlalchemy import select
        from app.db.models import User
        u = (await db.execute(select(User).where(User.username == username))).scalar_one()
        e = (await db.execute(select(MatchEntry).where(
            MatchEntry.match_id == match_id, MatchEntry.user_id == u.id))).scalar_one()
        return e.id


async def test_submit_happy_and_idempotent_replay(client, db_setup):
    match_id = await make_match(db_setup, client)
    rid = await _setup_open_round(db_setup, client, match_id)
    await login(client, "p_tiger_1", "pass-123456")
    _r = await client.post("/api/v1/auth/control/claim")
    assert _r.status_code == 200, _r.text
    ctl = _r.json()["control_token"]
    key = f"key-{uuid.uuid4().hex[:12]}"

    r = await client.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                          json={"option_id": "o1"}, headers={"Idempotency-Key": key, "X-Control-Token": ctl})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted" and body["receipt_id"]
    assert "is_correct" not in body, "回执不得泄露判分结果（§10.2）"

    # 同键重试 → 同一回执
    r2 = await client.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                           json={"option_id": "o1"}, headers={"Idempotency-Key": key, "X-Control-Token": ctl})
    assert r2.status_code == 200
    assert r2.json()["receipt_id"] == body["receipt_id"]

    # 同键不同内容 → 冲突，保留原答案
    r3 = await client.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                           json={"option_id": "o2"}, headers={"Idempotency-Key": key, "X-Control-Token": ctl})
    assert r3.status_code == 409
    assert r3.json()["code"] == "IDEMPOTENCY_CONFLICT"


async def test_submit_deadline_then_same_key_replay(client, db_setup):
    match_id = await make_match(db_setup, client)
    rid = await _setup_open_round(db_setup, client, match_id)
    await login(client, "p_dragon_1", "pass-123456")
    _r = await client.post("/api/v1/auth/control/claim")
    assert _r.status_code == 200, _r.text
    ctl = _r.json()["control_token"]
    key = f"key-{uuid.uuid4().hex[:12]}"
    r = await client.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                          json={"option_id": "o2"}, headers={"Idempotency-Key": key, "X-Control-Token": ctl})
    assert r.status_code == 200
    receipt = r.json()["receipt_id"]

    # 回合截止后：新键被拒
    factory = make_session_factory()
    async with factory() as db:
        r_ = await db.get(RoundInstance, rid)
        r_.deadline_at = (utcnow() - timedelta(seconds=1)).replace(tzinfo=None)
        await db.commit()
    r2 = await client.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                           json={"option_id": "o2"}, headers={"Idempotency-Key": f"kk-{uuid.uuid4().hex[:10]}", "X-Control-Token": ctl})
    assert r2.status_code == 409
    assert r2.json()["code"] == "ROUND_CLOSED"

    # 同键重试即便已截止 → 仍取回原回执（§10.6 第 1 行）
    r3 = await client.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                           json={"option_id": "o2"}, headers={"Idempotency-Key": key, "X-Control-Token": ctl})
    assert r3.status_code == 200
    assert r3.json()["receipt_id"] == receipt


async def test_submit_requires_control_token(client, db_setup):
    match_id = await make_match(db_setup, client)
    rid = await _setup_open_round(db_setup, client, match_id)
    await login(client, "p_tortoise_1", "pass-123456")
    # 不认领控制权直接提交
    r = await client.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                          json={"option_id": "o1"}, headers={"Idempotency-Key": f"kk-{uuid.uuid4().hex[:10]}"})
    assert r.status_code == 403
    assert r.json()["code"] == "CONTROL_TOKEN_INVALID"


async def test_submit_rejects_outsider_and_bad_option(client, db_setup):
    match_id = await make_match(db_setup, client)
    rid = await _setup_open_round(db_setup, client, match_id)

    # 无资格账号（学生）
    await login(client, "admin", "admin-pass-123")  # 管理员无参赛记录
    _r = await client.post("/api/v1/auth/control/claim")
    assert _r.status_code == 200, _r.text
    ctl = _r.json()["control_token"]
    r = await client.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                          json={"option_id": "o1"}, headers={"Idempotency-Key": f"kk-{uuid.uuid4().hex[:10]}", "X-Control-Token": ctl})
    assert r.status_code == 403
    assert r.json()["code"] == "NOT_ELIGIBLE"

    # 参赛者提交不存在的选项
    await login(client, "p_phoenix_1", "pass-123456")
    _r = await client.post("/api/v1/auth/control/claim")
    assert _r.status_code == 200, _r.text
    ctl = _r.json()["control_token"]
    r = await client.post(f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
                          json={"option_id": "o999"}, headers={"Idempotency-Key": f"kk-{uuid.uuid4().hex[:10]}", "X-Control-Token": ctl})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_OPTION"
