"""练习排行榜（B2）。

榜单口径（每条都必须能追溯到样本量，且与正式赛成绩**物理分区**）：

| 榜 | 排序 | 入榜门槛 | 说明 |
| --- | --- | --- | --- |
| 正确率榜 | 正确率 ↓，题量 ↓ | 累计 ≥ MIN_RANKED 题 | 防止「答 3 题全对」霸榜 |
| 速度榜 | 答对题的平均用时 ↑ | 累计 ≥ MIN_RANKED 题 | 只统计答对的题；显示中位数与样本量 |
| 连对榜 | 最长连续正确 ↑ | 一场内 ≥ MIN_RANKED 题 | 纯荣誉，学生最容易转发 |
| House 荣誉榜 | 练习总题量 ↓ | — | **明确标注不计入官方积分** |

隐私红线（D7/R07）：公开榜只输出展示名；匿名显示「枫禾学子 #nn」；
登录用户默认同样显示「枫禾学子」，只有自愿设置昵称时才显示昵称；
**永不输出**真实姓名、账号名、学号、班级、年级。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.db.models import GuestIdentity, House, PracticeAnswer, PracticeSession
from app.modules.practice.identity import ANONYMOUS_NAME, display_name_for

router = APIRouter(prefix="/api/v1", tags=["leaderboard"])

# 入榜门槛：低于这个题量的成绩不参与排名（否则 1 题 100% 会霸榜）
MIN_RANKED = 20
DEFAULT_LIMIT = 50


def display_name_of(row: PracticeSession, guest: GuestIdentity | None = None) -> str:
    """公开展示名。三种来源，按优先级：

    ① 学生**自愿设置的昵称**（存在 session.display_name）→ 用昵称；
    ② 匿名访客 → 「枫禾学子 #nn」（用访客自身的稳定序号）；
    ③ 登录用户未设昵称 → 「枫禾学子」（沿用 D7：登录用户同样不显示真实姓名）。

    为什么必须把 guest 传进来（这是一个实测到的真实缺陷）：
    先前只写 `row.display_name or ANONYMOUS_NAME`，而匿名会话建会话时 display_name 存的是
    NULL（那时还没有"用户"），于是**所有匿名学习者在上榜后都是同一个「枫禾学子」**——
    榜单上两条不同人的成绩看起来是同一个人，学生找不到自己，老师也无法核对。
    序号本来就在 `guest_identities.display_seq` 里，只是没有被读出来。
    """
    if row.display_name:
        return row.display_name
    if guest is not None:
        return display_name_for(guest)
    return ANONYMOUS_NAME


def _accuracy(row: PracticeSession) -> float:
    if row.answered_count <= 0:
        return 0.0
    return round(100.0 * row.correct_count / row.answered_count, 2)


@router.get("/leaderboard")
async def leaderboard(
    board: str = Query(default="accuracy", pattern="^(accuracy|speed|streak|house)$"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """公开排行榜。无需登录。"""
    if board == "house":
        return await _house_board(db, limit)
    return await _learner_board(db, board, limit)


async def _learner_board(db: AsyncSession, board: str, limit: int) -> dict:
    stmt = (select(PracticeSession)
            .where(PracticeSession.is_ranked.is_(True),
                   PracticeSession.answered_count >= MIN_RANKED))
    if board == "accuracy":
        # 正确率优先，同分时题量多者优先（样本更可信）
        stmt = stmt.order_by((PracticeSession.correct_count * 1000.0
                              / PracticeSession.answered_count).desc(),
                             PracticeSession.answered_count.desc(),
                             PracticeSession.total_ms.asc().nulls_last())
    elif board == "speed":
        # 速度榜的用时来自 practice_answers.elapsed_ms（只统计答对的题）
        stmt = stmt.order_by(PracticeSession.total_ms.asc().nulls_last(),
                             PracticeSession.correct_count.desc())
    else:  # streak
        stmt = stmt.order_by(PracticeSession.best_streak.desc(),
                             PracticeSession.correct_count.desc(),
                             PracticeSession.answered_count.desc())
    rows = (await db.execute(stmt.limit(limit))).scalars().all()

    # 速度榜需要「答对题的平均用时」——用一次分组查询补上，避免 N+1
    session_ids = [r.id for r in rows]
    speed: dict[str, tuple[float, int]] = {}
    if session_ids:
        agg = (await db.execute(
            select(PracticeAnswer.session_id,
                   func.avg(PracticeAnswer.elapsed_ms),
                   func.count())
            .where(PracticeAnswer.session_id.in_(session_ids),
                   PracticeAnswer.is_correct.is_(True),
                   PracticeAnswer.elapsed_ms.is_not(None))
            .group_by(PracticeAnswer.session_id)
        )).all()
        speed = {sid: (float(avg or 0), int(cnt)) for sid, avg, cnt in agg}

    # 匿名会话的序号来自访客身份：一页一次性取回，避免逐行查（同 _used_in_matches_map 的理由）
    guest_ids = [r.guest_id for r in rows if r.guest_id and not r.display_name]
    guests: dict[str, GuestIdentity] = {}
    if guest_ids:
        guests = {g.id: g for g in (await db.execute(
            select(GuestIdentity).where(GuestIdentity.id.in_(set(guest_ids)))
        )).scalars()}

    items = []
    for i, r in enumerate(rows, start=1):
        avg_ms, sample = speed.get(r.id, (0.0, 0))
        items.append({
            "rank": i,
            "display_name": display_name_of(r, guests.get(r.guest_id) if r.guest_id else None),
            "house_key": None,          # 登录用户的院只用于 House 榜聚合，不在个人榜暴露
            "answered_count": r.answered_count,
            "correct_count": r.correct_count,
            "accuracy": _accuracy(r),
            "best_streak": r.best_streak,
            "avg_correct_ms": int(avg_ms) if sample else None,
            "speed_sample": sample,
        })

    return {
        "board": board,
        "min_questions": MIN_RANKED,
        "items": items,
        "note": ("练习榜与正式赛成绩分区统计，不计入 House 官方积分。"
                 f"入榜门槛：累计作答 ≥ {MIN_RANKED} 题；速度仅统计答对的题，"
                 "单题用时不足 0.4 秒不计分（防连点）。"),
    }


async def _house_board(db: AsyncSession, limit: int) -> dict:
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}
    agg = (await db.execute(
        select(PracticeSession.house_id,
               func.count(func.distinct(PracticeSession.id)),
               func.coalesce(func.sum(PracticeSession.answered_count), 0),
               func.coalesce(func.sum(PracticeSession.correct_count), 0),
               func.count(func.distinct(PracticeSession.user_id)))
        .where(PracticeSession.is_ranked.is_(True), PracticeSession.house_id.is_not(None))
        .group_by(PracticeSession.house_id)
    )).all()
    rows = []
    for hid, sessions, answered, correct, learners in agg:
        h = houses.get(hid)
        if h is None:
            continue
        rows.append({
            "house_key": h.key, "house_name": h.name_cn,
            "color_light": h.color_light, "color_deep": h.color_deep,
            "sessions": int(sessions), "learners": int(learners),
            "answered_total": int(answered), "correct_total": int(correct),
            "accuracy": round(100.0 * correct / answered, 2) if answered else 0.0,
        })
    rows.sort(key=lambda r: (-r["answered_total"], r["house_key"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return {
        "board": "house",
        "items": rows[:limit],
        "note": ("House 荣誉榜只统计登录用户完成的练习量，用于展示参与度，"
                 "不计入 House 官方积分（官方积分只在正式赛中产生）。"),
    }
