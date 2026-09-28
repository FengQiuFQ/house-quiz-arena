"""场外援助（§5）：队长求援 + 智囊席建议（+ 管理员代录观众举牌）。

- 服务端事务扣除 House 额度；重试不重复扣（回合+House 唯一约束）。
- 求援窗口 = 展示期前段（display_at 起 aid_window_seconds），窗口内所有选手可读题不可提交；
  顾问须在窗口结束前提交建议，仅本 House 可见。
- 顾问不能看到标准答案（接口根本不返回 correct 字段）。
"""
from datetime import timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, require_admin
from app.core.errors import AppError, ErrorCode
from app.core.security import new_id
from app.db.base import as_utc, utcnow
from app.db.models import (
    AidRequest,
    House,
    Match,
    MatchEntry,
    MatchHouseState,
    MatchQuestion,
    QuestionVersion,
    RoundInstance,
    User,
    ROUND_DISPLAYING,
    # ⚠️ ROUND_OPEN 是 `_context()` 里抢答分支要用的常量。
    # 它曾经**漏在导入列表外面**，而抢答赛（display_seconds=0，本系统的默认赛制）
    # 一定会走到 `(ROUND_DISPLAYING, ROUND_OPEN)` 这一行 →
    # `NameError: name 'ROUND_OPEN' is not defined` → 500 INTERNAL_ERROR。
    # 也就是说：**抢答赛里的"呼叫智囊"从来没能成功过一次**，而界面只显示"服务器内部错误"，
    # 现场会以为是自己点错了。这个缺陷是靠 _verify/watch1.cjs 第 20 条
    # （真实点击 + 真实接口）才暴露出来的 —— 单元测试只覆盖了读题赛那条分支。
    ROUND_OPEN,
)
from app.modules.audit.service import audit
from app.realtime import protocol
from app.realtime.hub import hub

router = APIRouter(prefix="/api/v1", tags=["assistance"])


class AidRequestIn(BaseModel):
    round_instance_id: str


class AdviceIn(BaseModel):
    option_id: str = Field(min_length=1, max_length=16)
    note: str | None = Field(default=None, max_length=200)


async def _context(db: AsyncSession, match_id: str, round_instance_id: str):
    match = await db.get(Match, match_id)
    round_ = await db.get(RoundInstance, round_instance_id)
    if match is None or round_ is None or round_.match_id != match_id:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛或回合不存在。")
    if not match.aid_enabled:
        raise AppError(409, ErrorCode.AID_NOT_AVAILABLE, "本场比赛未启用场外援助。")

    # 求援窗口跟着**赛制**走：
    #   抢答（display_seconds == 0）：题目一出现就能答，没有"只能看"的读题期，
    #     因此窗口从题目出现（display_at）起算，长度仍是 aid_window_seconds。
    #     旧实现要求回合处于 displaying 阶段，而抢答回合永远是 open —— 结果是
    #     "启用了援助但永远点不动"，现场会以为功能坏了。
    #   读题赛（display_seconds > 0）：窗口就是读题期内，保持原样。
    buzz = int(match.display_seconds or 0) <= 0
    allowed_status = (ROUND_DISPLAYING, ROUND_OPEN) if buzz else (ROUND_DISPLAYING,)
    if round_.status not in allowed_status or round_.display_at is None:
        raise AppError(409, ErrorCode.AID_WINDOW_CLOSED,
                       "不在求援阶段（题目已结束或尚未出现）。")
    window_end = as_utc(round_.display_at) + timedelta(seconds=match.aid_window_seconds)
    return match, round_, window_end


@router.post("/matches/{match_id}/aid-requests")
async def request_aid(match_id: str, body: AidRequestIn,
                      user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    match, round_, window_end = await _context(db, match_id, body.round_instance_id)
    entry = (await db.execute(select(MatchEntry).where(
        MatchEntry.match_id == match_id, MatchEntry.user_id == user.id))).scalar_one_or_none()
    if entry is None or not entry.is_captain:
        raise AppError(403, ErrorCode.NOT_ELIGIBLE, "只有本 House 队长可以触发求援。")
    if as_utc(utcnow()) > window_end:
        raise AppError(409, ErrorCode.AID_WINDOW_CLOSED, "求援窗口已关闭。")

    existing = (await db.execute(select(AidRequest).where(
        AidRequest.round_instance_id == round_.id,
        AidRequest.house_id == entry.house_id))).scalar_one_or_none()
    if existing is not None:
        return {"aid_request_id": existing.id, "status": existing.status, "replayed": True}

    st = (await db.execute(select(MatchHouseState).where(
        MatchHouseState.match_id == match_id,
        MatchHouseState.house_id == entry.house_id))).scalar_one_or_none()
    if st is None or st.aid_remaining <= 0:
        raise AppError(409, ErrorCode.AID_NOT_AVAILABLE, "本 House 本场求援额度已用完。")

    st.aid_remaining -= 1  # 事务扣减；下方唯一约束防重试重复扣
    # 先取出要用的标量再提交：`db.commit()` 会让 ORM 实例**过期**，
    # 之后访问 `st.aid_remaining` / `aid.id` 会触发一次隐式重新加载 —— 在 async 会话里
    # 那直接抛 MissingGreenlet（500）。学生按下"求援"看到的就是"服务器内部错误"。
    remaining_after = st.aid_remaining
    aid = AidRequest(id=new_id(), match_id=match_id, round_instance_id=round_.id,
                     house_id=entry.house_id, requested_by_entry_id=entry.id)
    db.add(aid)
    house_key = None
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        again = (await db.execute(select(AidRequest).where(
            AidRequest.round_instance_id == round_.id,
            AidRequest.house_id == entry.house_id))).scalar_one_or_none()
        if again is not None:
            return {"aid_request_id": again.id, "status": again.status, "replayed": True}
        raise
    aid_id = aid.id
    houses = {h.id: h.key for h in (await db.execute(select(House))).scalars()}
    house_key = houses.get(entry.house_id)
    hub.broadcast(match_id, protocol.message(protocol.EV_AID_UPDATED, match_id, {
        "house": house_key, "phase": "requested",
        "remaining": remaining_after,
    }))
    return {"aid_request_id": aid_id, "status": "pending"}


async def _apply_advice(db: AsyncSession, aid: AidRequest, body: AdviceIn,
                        advisor_user: User) -> dict:
    match, round_, window_end = await _context(db, aid.match_id, aid.round_instance_id)
    if as_utc(utcnow()) > window_end:
        raise AppError(409, ErrorCode.AID_WINDOW_CLOSED, "求援窗口已关闭，无法提交建议。")
    mq = await db.get(MatchQuestion, round_.match_question_id)
    v = await db.get(QuestionVersion, mq.version_id)
    if body.option_id not in {o["option_id"] for o in v.options_json}:
        raise AppError(422, ErrorCode.INVALID_OPTION, "建议的选项不属于本题。")
    if aid.status == "answered":
        return {"aid_request_id": aid.id, "status": aid.status, "replayed": True}
    # 提交前把要用的标量全部取出：commit() 之后 ORM 属性会过期，
    # 在 async 会话里再访问会触发隐式加载并抛 MissingGreenlet（500）。
    aid_id = aid.id
    house_id = aid.house_id
    aid.advice_option_id = body.option_id
    aid.advice_note = body.note
    aid.advised_by_user_id = advisor_user.id
    aid.advised_at = utcnow()
    aid.status = "answered"
    await audit(db, advisor_user.id, "aid.advice", "aid_request", aid_id,
                {"option_id": body.option_id})
    await db.commit()
    house = await db.get(House, house_id)
    # 建议只发给本 House 的连接（§5）
    hk = house.key if house else None
    hub.broadcast(match.id, protocol.message(protocol.EV_AID_UPDATED, match.id, {
        "house": hk, "phase": "advised",
        "advice": {"option_id": body.option_id, "note": body.note},
    }), predicate=lambda c: getattr(c, "house_key", None) == hk or c.role == "admin")
    return {"aid_request_id": aid_id, "status": "answered"}


@router.post("/aid-requests/{aid_id}/advice")
async def give_advice(aid_id: str, body: AdviceIn, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    aid = await db.get(AidRequest, aid_id)
    if aid is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "求援记录不存在。")
    if user.role != "advisor" or user.house_id != aid.house_id:
        raise AppError(403, ErrorCode.FORBIDDEN, "只有本 House 授权智囊席可以提交建议。")
    return await _apply_advice(db, aid, body, user)


@router.post("/admin/matches/{match_id}/aid-requests/{aid_id}/advice")
async def record_advice(match_id: str, aid_id: str, body: AdviceIn,
                        admin: User = Depends(require_admin),
                        db: AsyncSession = Depends(get_db)) -> dict:
    """观众举牌方案的主持人代录（§5 降级路径）；录入的是建议，不是替选手作答。"""
    aid = await db.get(AidRequest, aid_id)
    if aid is None or aid.match_id != match_id:
        raise AppError(404, ErrorCode.NOT_FOUND, "求援记录不存在。")
    return await _apply_advice(db, aid, body, admin)
