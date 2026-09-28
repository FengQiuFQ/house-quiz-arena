"""时间契约：任何来源的时间都必须以 **UTC ISO8601（带 Z）** 对外输出。

回归背景（真实缺陷，PostgreSQL 专有，演示赛四院全 0 分）
--------------------------------------------------------
时间列一度是 `DateTime(timezone=True)`（timestamptz），而应用写入的是 **naive** 时间。
asyncpg 对带时区列会引入"会话时区"这一层解释（本机 UTC+8）：
    · 写：naive 被当成会话时区的墙上时间落库；
    · 读：返回**会话时区**表示的 aware 时间。
SQLite 全程按文本存 naive，所以整个 SQLite 测试套件都发现不了它。
旧 `as_utc()` 又把 aware 值原样返回，经 `replace("+00:00","Z")` 输出后
"带 +08:00 的字符串被当成 UTC"——库里 16:56 的截止时间读成 08:56Z，
比服务端时间早 8 小时，**所有提交都被判"作答通道已关闭"**。

修法（不再靠换算公式）：把两种后端统一成 `timestamp without time zone`，
口径只有一个——入库即 UTC 墙上时间；代码侧读写都不带 tzinfo。
迁移见 migrations/versions/c9a6e3b70f48。

下面这些用例守住这条契约：无论喂进来的是 naive、真 UTC aware，
还是被会话时区污染过的 aware 值，输出都必须是同一个 UTC 时刻。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.base import as_utc, iso_z, server_time_iso, utcnow
from tests.conftest import advance_factory, command, login, make_client, make_match

# 用固定偏移而不是 ZoneInfo：不依赖 Windows 上未必存在的 tzdata。
SHANGHAI = timezone(timedelta(hours=8))      # asyncpg 在本机部署时的默认会话时区
NEW_YORK = timezone(timedelta(hours=-4))     # 反向偏移，确保不是"凑巧减对"


def parse_z(s: str) -> datetime:
    """严格解析契约格式：必须以 Z 结尾，且不含其它偏移写法。"""
    assert isinstance(s, str), f"时间字段应为字符串，实际 {type(s)}"
    assert s.endswith("Z"), f"时间字段必须以 Z 结尾（UTC），实际 {s!r}"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- 单元层

def test_as_utc_labels_without_reinterpreting():
    """核心契约：as_utc **只贴 UTC 标签，不做时区换算**。

    库内约定是"入库即 UTC 墙上时间"，所以同一串数字必须原样读出来。
    这条断言的意义在于：一旦有人把 `as_utc` 改回 `astimezone(utc)`，
    本机（系统时钟是本地墙上时间而时区标成 UTC）就会出现 8 小时整体偏移。
    """
    wall = datetime(2026, 9, 25, 16, 44, 56)
    assert as_utc(wall) == datetime(2026, 9, 25, 16, 44, 56, tzinfo=timezone.utc)
    # 带 +08:00 的值同样按"墙上时间就是 UTC 墙上时间"处理
    assert as_utc(wall.replace(tzinfo=SHANGHAI)) == datetime(2026, 9, 25, 16, 44, 56,
                                                             tzinfo=timezone.utc)
    assert as_utc(wall.replace(tzinfo=NEW_YORK)) == datetime(2026, 9, 25, 16, 44, 56,
                                                             tzinfo=timezone.utc)


def test_as_utc_is_idempotent_and_handles_both_kinds():
    naive_utc = datetime(2026, 9, 25, 8, 44, 56)           # SQLite 存法
    aware_utc = naive_utc.replace(tzinfo=timezone.utc)
    aware_sh = naive_utc.replace(tzinfo=SHANGHAI)          # 旧 timestamptz 列的返回形态
    for dt in (naive_utc, aware_utc, aware_sh):
        assert as_utc(dt) == aware_utc
        assert as_utc(as_utc(dt)) == aware_utc
        assert as_utc(dt).tzinfo is timezone.utc
    assert as_utc(None) is None


def test_aware_and_naive_never_compared_by_accident():
    """往返一次后仍是 aware，可与 aware 时间安全比较（naive/aware 混比会 TypeError）。"""
    stored = datetime(2026, 9, 25, 16, 44, 56, tzinfo=SHANGHAI)
    now = datetime.now(timezone.utc)
    assert as_utc(stored) < now   # 不抛 TypeError


def test_iso_z_outputs_canonical_utc_z():
    aware = datetime(2026, 9, 25, 16, 44, 56, 123456, tzinfo=SHANGHAI)
    assert iso_z(aware) == "2026-09-25T16:44:56Z"          # 数字原样，去掉微秒并写 Z
    assert iso_z(datetime(2026, 9, 25, 16, 44, 56)) == "2026-09-25T16:44:56Z"
    assert iso_z(None) is None
    # 任何非 UTC 时区都不得泄漏到载荷里（列类型修好后这里也不该再出现偏移写法）
    assert "+08:00" not in iso_z(aware)
    assert iso_z(datetime(2026, 9, 25, 4, 44, 56, tzinfo=NEW_YORK)) == "2026-09-25T04:44:56Z"


def test_server_time_is_utc_and_close_to_now():
    d = parse_z(server_time_iso())
    assert abs((datetime.now(timezone.utc) - d).total_seconds()) < 5


def test_utcnow_is_naive_utc():
    n = utcnow()
    assert n.tzinfo is None
    assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - n).total_seconds()) < 5


# ------------------------------------------------- HTTP 层（SQLite 可跑；PG 上即为端到端把关）

async def _round_times(snapshot: dict) -> dict:
    for key in ("round", "current_round"):
        if isinstance(snapshot.get(key), dict) and snapshot[key].get("open_at"):
            return snapshot[key]
    raise AssertionError(f"快照里没有回合时间字段，实际键：{sorted(snapshot)}")


@pytest.mark.asyncio
async def test_round_window_is_ordered_against_server_time(db_setup, client):
    """作答窗口：open_at ≤ server_time ≤ deadline_at。

    这条断言在 PostgreSQL 上正是失败的那一条（截止时间被写成 8 小时前，
    于是所有提交都收到"作答通道已关闭"）。窗口在这里显式设定，
    避免依赖推进时序，测的是时区换算而不是调度精度。
    """
    match_id = await make_match(db_setup, client, question_count=1,
                                display=1, answer=30)
    r = await command(client, match_id, "start_match")
    assert r.status_code == 200, r.text
    await advance_factory()(match_id, utcnow())          # 产生第一回合（展示期）

    from datetime import timedelta as _td

    from app.db.models import Match, RoundInstance, ROUND_OPEN
    from app.db.session import make_session_factory
    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        r_ = await db.get(RoundInstance, m.current_round_id)
        now = utcnow()
        r_.status = ROUND_OPEN
        r_.display_at = now - _td(seconds=3)
        r_.open_at = now - _td(seconds=1)
        r_.deadline_at = now + _td(seconds=120)
        await db.commit()

    pc = make_client()
    try:
        await login(pc, "p_tiger_1", "pass-123456")
        snap = (await pc.get(f"/api/v1/matches/{match_id}/snapshot")).json()
    finally:
        await pc.aclose()

    rd = await _round_times(snap)
    server = parse_z(rd["server_time"])
    open_at, deadline = parse_z(rd["open_at"]), parse_z(rd["deadline_at"])
    assert open_at <= server <= deadline, (
        f"回合窗口与服务端时间不自洽：open={open_at} server={server} deadline={deadline}")
    assert (deadline - open_at).total_seconds() > 0
    # 展示时间早于开放时间（同一时区换算下才成立）
    assert parse_z(rd["display_at"]) <= open_at


@pytest.mark.asyncio
async def test_public_home_time_fields_are_utc_z(client):
    """首页/公开载荷的时间字段同样只能是 UTC 带 Z。"""
    payload = (await client.get("/api/v1/home")).json()

    def walk(node, path="$"):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and ("_at" in path.rsplit(".", 1)[-1]):
            parse_z(node)          # 断言：必须 Z 结尾

    walk(payload)


@pytest.mark.asyncio
async def test_submission_immediately_after_open_is_accepted(db_setup, client):
    """刚开放的**第一份**提交必须被受理。

    复现的是真实缺陷：回合的 `open_at` 落在未来 1 秒（控制器还没轮到它），
    控制器到点把它翻成 open 的那一刻，客户端拿到的快照里 phase 已经是 open，
    于是立刻提交 —— 旧实现把 open_at 设成"控制器当前时间"，而 received_at 是**秒级**的，
    open_at 带微秒时就会比 received_at "晚"，第一份提交被判 too_early。
    PostgreSQL 演示赛四院全 0 分就是这么来的。

    修法：放开时把 open_at 收敛到 `now - round_tick`（并截断到秒），
    保证任何"已经看到 open"的客户端提交都不会被这条判定挡住。
    """
    import uuid

    import asyncio

    from sqlalchemy import select as _select

    from app.db.models import Match, RoundInstance
    from app.db.session import make_session_factory

    match_id = await make_match(db_setup, client, question_count=1, display=1, answer=60)
    r = await command(client, match_id, "start_match")
    assert r.status_code == 200, r.text
    await advance_factory()(match_id, utcnow())           # 产生第一回合（展示期）

    # 把开放时间推到 1 秒后，模拟"控制器还没轮到翻状态"
    factory = make_session_factory()
    async with factory() as db:
        m = await db.get(Match, match_id)
        rd_row = await db.get(RoundInstance, m.current_round_id)
        now = utcnow()
        rd_row.open_at = now + timedelta(seconds=1)
        rd_row.deadline_at = now + timedelta(seconds=30)
        await db.commit()

    # 控制器此刻运行：它的时钟还没到 open_at → 不应翻状态
    await advance_factory()(match_id, utcnow())
    async with factory() as db:
        m = await db.get(Match, match_id)
        assert (await db.get(RoundInstance, m.current_round_id)).status == "displaying"

    # 到点后控制器运行 → 翻成 open，并把 open_at 收敛到"已开放"的时刻。
    #
    # 用**重试推进**而不是一次注入固定偏移：整秒截断后"刚好到点"的边界取决于
    # 相差多少毫秒，一次注入会偶发差一拍（本项目实测过这种 flake）。
    # 注意这里注入的是 utcnow()（不加偏移）：推进得太远会让 open_at 被算成"未来"，
    # 反而复现不出"刚开放就提交"的场景。要的是控制器"刚好到点"的那一刻。
    for _ in range(10):
        await advance_factory()(match_id, utcnow())
        async with factory() as db:
            m = await db.get(Match, match_id)
            rd_row = await db.get(RoundInstance, m.current_round_id)
            if rd_row.status == "open":
                break
        await asyncio.sleep(0.2)
    else:
        raise AssertionError("控制器连续多次推进后仍未把回合翻成 open")

    await login(client, "p_dragon_1", "pass-123456")
    claim = await client.post("/api/v1/auth/control/claim")
    assert claim.status_code == 200, claim.text
    ctl = claim.json()["control_token"]

    snap = (await client.get(f"/api/v1/matches/{match_id}/snapshot")).json()
    rd = await _round_times(snap)
    assert rd["phase"] == "open", f"到点后应已开放：{rd}"
    # 不断言 "open_at <= server_time"：整秒截断后两者可能相差不到 1 秒，
    # 而真正要守住的契约是**下一行**——已经看到 open 的客户端立刻提交必须被受理。
    # （用"能不能提交成功"来验证时序，比拿两个截断过的时间戳比大小更贴近事实。）

    rid = rd["round_instance_id"]
    sub = await client.post(
        f"/api/v1/matches/{match_id}/rounds/{rid}/answers",
        json={"option_id": "o1"},
        headers={"Idempotency-Key": f"key-{uuid.uuid4().hex[:12]}", "X-Control-Token": ctl})
    assert sub.status_code == 200, f"开放后立刻提交被拒：{sub.text}"
    assert sub.json()["status"] == "accepted"
