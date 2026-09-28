"""测试环境：独立 SQLite 数据库；先设环境变量再导入应用（settings 在导入期读取）。"""
import asyncio
import os
import os as _os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 测试库路径可用 MGUS_TEST_DB_PATH 覆盖：默认固定，便于本地调试；
# 需要**并行跑两套测试**（例如同时改两个模块）时各自指定一个文件名，避免互相清库。
# 只取文件名（丢弃传入的目录部分）：这样 `tests/_x.db` 与 `_x.db` 都落在 tests/ 下，
# 不会拼出一个不存在的多级目录导致 sqlite 直接打不开库。
DB_PATH = Path(__file__).parent / (Path(_os.environ["MGUS_TEST_DB_PATH"]).name
                                   if _os.environ.get("MGUS_TEST_DB_PATH") else "_test_mgus.db")
if DB_PATH.exists() and not _os.environ.get("KEEP_TEST_DB"):
    DB_PATH.unlink()
os.environ["MGUS_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"
os.environ["MGUS_RATE_LIMIT_LOGIN_PER_MIN"] = "1000"
os.environ["MGUS_RATE_LIMIT_LOGIN_IP_PER_MIN"] = "100000"
os.environ["MGUS_RATE_LIMIT_LOGIN_FAIL_PER_MIN"] = "1000"
os.environ["MGUS_RATE_LIMIT_SUBMIT_PER_MIN"] = "1000"
os.environ["MGUS_COOKIE_SECURE"] = "0"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.security import hash_password, new_id  # noqa: E402
from app.db.base import utcnow  # noqa: E402
from app.db.base import Base, utcnow  # noqa: E402
from app.db.models import (  # noqa: E402
    House, Match, Question, QuestionVersion, User,
    POOL_OFFICIAL, ROLE_ADMIN, ROLE_STUDENT,
)
from app.db.session import get_engine, make_session_factory, reset_engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest_asyncio.fixture(scope="session")
async def db_setup():
    reset_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory()
    async with factory() as db:
        houses = {}
        # 与 scripts/seed.py 的正式名称保持一致（四象：青龙/白虎/玄武/朱雀）。
        # key 是技术标识，永远不变；这里只是让测试库的展示名与现场一致。
        for key, cn, en in [("tiger", "白虎", "White Tiger"), ("dragon", "青龙", "Azure Dragon"),
                            ("tortoise", "玄武", "Black Tortoise"), ("phoenix", "朱雀", "Vermilion Bird")]:
            h = House(id=new_id(), key=key, name_cn=cn, name_en=en,
                      color_light="#DDDDDD", color_deep="#666666")
            db.add(h)
            houses[key] = h
        admin = User(id=new_id(), username="admin", password_hash=hash_password("admin-pass-123"),
                     role=ROLE_ADMIN)
        db.add(admin)
        players = {}
        for key in houses:
            for i in (1, 2):
                u = User(id=new_id(), username=f"p_{key}_{i}",
                         password_hash=hash_password("pass-123456"),
                         role=ROLE_STUDENT, house_id=houses[key].id)
                db.add(u)
                players[f"{key}_{i}"] = u
        await db.commit()

        # 题目：correct = o1
        qids = []
        for n in range(6):
            q = Question(id=new_id(), subject="科学常识", difficulty=1, language="zh",
                         pool=POOL_OFFICIAL, status="published", prompt_hash=f"hash{n}")
            v = QuestionVersion(id=new_id(), question_id=q.id, version_no=1,
                                prompt=f"测试题 {n}？",
                                options_json=[{"option_id": "o1", "text": "对"},
                                              {"option_id": "o2", "text": "错"}],
                                correct_option_id="o1", review_state="approved")
            db.add_all([q, v])
            q.current_version_id = v.id
            qids.append(q.id)
        await db.commit()
    yield {"houses": houses, "admin": admin, "players": players, "question_ids": qids}
    await engine.dispose()
    if DB_PATH.exists() and not _os.environ.get("KEEP_TEST_DB"):
        DB_PATH.unlink()


async def _inject_csrf(request) -> None:
    """模拟真实浏览器：写操作自动带上 X-CSRF-Token（值＝会话 Cookie）。

    生产前端由 api/client.ts 统一注入；测试里用 httpx 事件钩子等价模拟，
    使全部既有用例在开启 CSRF 校验后无需逐个改造，也顺带验证了校验逻辑本身。
    """
    if request.method.upper() in ("GET", "HEAD", "OPTIONS"):
        return
    session = request.headers.get("cookie", "")
    token = ""
    for part in session.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "mgus_session":
            token = v
            break
    if token and not request.headers.get("X-CSRF-Token"):
        request.headers["X-CSRF-Token"] = token


def make_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app),
                       base_url="http://test", follow_redirects=False,
                       event_hooks={"request": [_inject_csrf]})


@pytest_asyncio.fixture
async def client(db_setup):
    async with make_client() as c:
        yield c


async def login(client: AsyncClient, username: str, password: str) -> dict:
    """登录并处理单活跃会话冲突：force 接管 → （比赛中）管理员核验吊销后重登。

    对应产品规则 §16.2：非比赛可自行 force；比赛中换机走主持人核验路径。
    """
    r = await client.post("/api/v1/auth/login",
                          json={"username": username, "password": password})
    if r.status_code == 409 and r.json().get("code") == "SESSION_ACTIVE":
        r = await client.post("/api/v1/auth/login",
                              json={"username": username, "password": password, "force": True})
    if r.status_code == 409 and r.json().get("code") == "SESSION_ACTIVE":
        # 正在比赛中：模拟主持人核验换机（直接吊销旧会话与控制权租约）
        from app.db.models import ControlLease, User as UserModel, UserSession
        factory = make_session_factory()
        async with factory() as db:
            u = (await db.execute(select(UserModel).where(UserModel.username == username))).scalar_one()
            if u.active_session_id:
                old = await db.get(UserSession, u.active_session_id)
                if old is not None:
                    old.revoked_at = utcnow()
                u.active_session_id = None
            lease = await db.get(ControlLease, u.id)
            if lease is not None:
                await db.delete(lease)
            await db.commit()
        r = await client.post("/api/v1/auth/login",
                              json={"username": username, "password": password})
    assert r.status_code == 200, f"{username} 登录失败: {r.text}"
    return r.json()


async def make_match(db_setup, client: AsyncClient, question_count: int = 2,
                     fixed_k: int = 1, display: int = 1, answer: int = 60,
                     with_roster: bool = True) -> str:
    await login(client, "admin", "admin-pass-123")
    r = await client.post("/api/v1/admin/matches", json={
        "title": f"[DEMO] 测试赛 {new_id()[:6]}", "mode": "fixed_k", "fixed_k": fixed_k,
        "planned_questions": question_count,
        "question_ids": db_setup["question_ids"][:question_count],
        "display_seconds": display, "answer_seconds": answer, "interlude_seconds": 0,
    })
    assert r.status_code == 200, r.text
    match_id = r.json()["match_id"]
    if not with_roster:
        return match_id
    # 开大厅 + 签到（每 House 前 K 名）。
    # 注意这里**不再**预写 MatchEligibility：参赛资格已改为"按学院"，
    # 绑定学院即可签到；如果这里还偷偷加资格，测试就会掩盖"资格门又回来了"这类回归。
    r = await command(client, match_id, "open_lobby")
    assert r.status_code == 200, r.text
    for key in db_setup["houses"]:
        for i in (1, 2)[:fixed_k if fixed_k <= 2 else 2]:
            username = f"p_{key}_{i}"
            pc = make_client()
            await login(pc, username, "pass-123456")
            r = await pc.post(f"/api/v1/matches/{match_id}/check-in")
            assert r.status_code == 200, f"{username}: {r.text}"
            await pc.aclose()
    return match_id


async def command(client: AsyncClient, match_id: str, action: str, payload: dict | None = None,
                  command_id: str | None = None) -> dict:
    # 读当前 state_version
    me = await client.get("/api/v1/auth/me")
    r = await client.get("/api/v1/admin/matches")
    items = r.json()["items"]
    sv = next(m["state_version"] for m in items if m["id"] == match_id)
    r = await client.post(f"/api/v1/admin/matches/{match_id}/commands", json={
        "command_id": command_id or f"cmd-{new_id()[:12]}",
        "action": action, "expected_state_version": sv, "payload": payload or {}})
    return r


def advance_factory():
    """直接驱动控制器逻辑（绕过真实时钟等待）。"""
    from app.modules.competitions.service import advance_match
    factory = make_session_factory()

    async def _advance(match_id: str, now=None):
        async with factory() as db:
            m = await db.get(Match, match_id)
            broadcasts = await advance_match(db, m, now=now)
            await db.commit()
            return broadcasts, m.status
    return _advance
