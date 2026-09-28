"""结果查询（§8、§10.4）：

- 公开端点只返回当前**已发布**版本；未发布一律 RESULT_NOT_PUBLIC（含管理员——预览走专用端点）。
- 管理员预览只允许 calculated 之后的主机/后台查看，且响应带 preview 标记、不进公开缓存。
"""
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_admin
from app.core.errors import AppError, ErrorCode
from app.db.models import Match, User
from app.modules.scoring.service import latest_calculated_version, results_payload

router = APIRouter(prefix="/api/v1", tags=["results"])


@router.get("/matches/{match_id}/results")
async def public_results(match_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    rv = None
    if match.published_result_version_id:
        from app.db.models import ResultVersion
        rv = await db.get(ResultVersion, match.published_result_version_id)
    if rv is None or rv.published_at is None:
        raise AppError(403, ErrorCode.RESULT_NOT_PUBLIC,
                       "成绩尚未公布。公布时间以现场主持公告为准。")
    payload = await results_payload(db, match, rv)
    payload["entry_point"] = "published"
    return payload


@router.get("/admin/matches/{match_id}/preview-results")
async def preview_results(match_id: str, admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    rv = await latest_calculated_version(db, match)
    if rv is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "本场尚未生成成绩版本（需先 calculate）。")
    payload = await results_payload(db, match, rv)
    payload["entry_point"] = "preview"
    payload["published"] = rv.published_at is not None
    return payload


@router.get("/admin/matches/{match_id}/participants")
async def participant_detail(match_id: str, admin: User = Depends(require_admin),
                             db: AsyncSession = Depends(get_db)) -> dict:
    """复核用个人明细（仅后台；不进入任何公开缓存）。"""
    from app.db.models import MatchEntry, ParticipantResult, ResultVersion, User as U
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    rv = await latest_calculated_version(db, match)
    if rv is None:
        return {"items": [], "result_version": None}
    rows = (await db.execute(
        select(ParticipantResult, MatchEntry, U)
        .join(MatchEntry, MatchEntry.id == ParticipantResult.entry_id)
        .join(U, U.id == ParticipantResult.user_id)
        .where(ParticipantResult.result_version_id == rv.id)
    )).all()
    from app.db.models import House as HouseModel
    houses = {h.id: h.key for h in (await db.execute(select(HouseModel))).scalars()}
    return {"result_version": rv.version_no, "items": [{
        "username": u.username, "house": houses.get(p.house_id), "alias": e.display_alias,
        "correct": p.correct_count, "answered": p.answered_count,
        "valid_questions": p.valid_question_count,
        "score": round(100.0 * p.score_num / p.score_den, 2),
        "scoring_status": e.scoring_status, "exclusion_reason": e.exclusion_reason,
    } for p, e, u in rows]}
