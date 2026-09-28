"""公开首页与 House 详情（§2）。

只输出**已发布**结果（published_at 非空且为 match.published_result_version_id 指向的版本）；
无历史数据时返回 demo 标记的演示数据（前端必须显著标注）；永不输出真实姓名。
"""
import math

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.db.base import iso_z, server_time_iso
from app.db.models import House, HouseResult, Match, MatchEntry, ResultVersion


def _iso(dt) -> str | None:
    """统一 UTC ISO8601（带 Z）。前端用它 + server_time 校准倒计时。

    ⚠ 不要在这里自己做 `as_utc(dt).isoformat().replace("+00:00","Z")`：
    PostgreSQL 的 timestamptz 会按**会话时区**返回（本机 UTC+8），
    那样写出来的字符串带着 +08:00 却被当成 UTC，全站时间线会整体偏 8 小时。
    """
    return iso_z(dt)

router = APIRouter(prefix="/api/v1", tags=["public"])

HOUSE_ORDER = ["tiger", "dragon", "tortoise", "phoenix"]


def _score(num: int, den: int) -> float:
    """存储的分数为精确分数（已含 ×100 系数，如 200/2=100 分），显示才取两位小数。"""
    return round(num / den, 2) if den else 0.0


@router.get("/home")
async def home(db: AsyncSession = Depends(get_db)) -> dict:
    houses = (await db.execute(
        select(House).where(House.is_active.is_(True)).order_by(House.created_at)
    )).scalars().all()

    # 已发布版本：先取各 match 已发布的 result_version
    published = (await db.execute(
        select(ResultVersion, Match)
        .join(Match, Match.id == ResultVersion.match_id)
        .where(ResultVersion.published_at.is_not(None),
               ResultVersion.id == Match.published_result_version_id)
        .order_by(ResultVersion.published_at.desc())
    )).all()

    per_house: dict[str, list] = {}
    for rv, m in published:
        hrs = (await db.execute(
            select(HouseResult).where(HouseResult.result_version_id == rv.id)
        )).scalars().all()
        for hr in hrs:
            per_house.setdefault(hr.house_id, []).append({
                "match_id": m.id, "match_title": m.title, "version_no": rv.version_no,
                "score": _score(hr.score_num, hr.score_den), "rank": hr.rank,
                "is_tie": hr.is_tie, "participants": hr.participant_count,
                "published_at": _iso(rv.published_at), "is_demo": rv.is_demo,
            })

    # 进行中的比赛状态（不泄露任何未揭晓比分）
    live = (await db.execute(
        select(Match).where(Match.status.in_(["lobby", "running", "paused"]))
        .order_by(Match.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    cards = []
    has_published = bool(published)
    for h in houses:
        hist = per_house.get(h.id, [])
        latest = hist[0] if hist else None
        trend = [{"score": it["score"]} for it in list(reversed(hist))[:5]]
        honors = sum(1 for it in hist if it["rank"] == 1 and not it["is_tie"])
        cards.append({
            "key": h.key, "name_cn": h.name_cn, "name_en": h.name_en,
            "color_light": h.color_light, "color_deep": h.color_deep,
            "latest": latest, "trend": trend, "championships": honors,
        })

    # 进行中/待开赛比赛的展示信息。首页据此在三种状态间切换：
    #   lobby   → 倒计时（tension → 到点切按钮）
    #   running → 立即参与竞赛
    #   paused  → 中断提示
    # `server_time` 用于客户端校准倒计时（不信任本机时钟）。
    # `start_at` 可能为空（主持人未设预定时间）→ 前端显示"即将开始"而不是伪造倒计时。
    live_out = None
    if live is not None:
        checked_in = (await db.execute(
            select(func.count()).select_from(MatchEntry)
            .where(MatchEntry.match_id == live.id, MatchEntry.checked_in_at.is_not(None))
        )).scalar_one()
        live_out = {
            "match_id": live.id,
            "title": live.title,
            "status": live.status,
            "mode": live.mode,
            "planned_questions": live.planned_questions,
            "answer_seconds": live.answer_seconds,
            "display_seconds": live.display_seconds,
            "start_at": _iso(live.start_at),
            "checked_in": int(checked_in),
            "server_time": server_time_iso(),
        }

    return {
        "has_published": has_published,
        "houses": cards,
        "live_match": live_out,
        # 无真实历史时前端以演示数据渲染空状态，必须保留 demo 标注（§2）
        "demo": not has_published,
    }


@router.get("/houses/{key}")
async def house_detail(key: str, db: AsyncSession = Depends(get_db)) -> dict:
    h = (await db.execute(select(House).where(House.key == key))).scalar_one_or_none()
    if h is None:
        from app.core.errors import AppError, ErrorCode
        raise AppError(404, ErrorCode.NOT_FOUND, "House 不存在。")

    rows = (await db.execute(
        select(HouseResult, ResultVersion, Match)
        .join(ResultVersion, ResultVersion.id == HouseResult.result_version_id)
        .join(Match, Match.id == ResultVersion.match_id)
        .where(HouseResult.house_id == h.id,
               ResultVersion.published_at.is_not(None),
               ResultVersion.id == Match.published_result_version_id)
        .order_by(ResultVersion.published_at.desc())
    )).all()

    history, trend_scores = [], []
    championships = 0
    for hr, rv, m in rows:
        score = _score(hr.score_num, hr.score_den)
        history.append({
            "match_id": m.id, "title": m.title, "published_at": _iso(rv.published_at),
            "score": score, "rank": hr.rank, "is_tie": hr.is_tie,
            "participants": hr.participant_count, "version_no": rv.version_no,
            "is_official": rv.is_official, "is_demo": rv.is_demo,
        })
        trend_scores.append(score)
        if hr.rank == 1 and not hr.is_tie:
            championships += 1

    avg_accuracy = round(sum(trend_scores) / len(trend_scores), 2) if trend_scores else None
    return {
        "key": h.key, "name_cn": h.name_cn, "name_en": h.name_en,
        "color_light": h.color_light, "color_deep": h.color_deep,
        "history": history, "championships": championships,
        "avg_score": avg_accuracy,
        "matches_count": len(history),
        "note": "平均分=已公布场次 House 官方分的算术平均；样本量见各场次参与人数。",
        "demo": not history,
    }
