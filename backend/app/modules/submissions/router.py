"""答案提交（§10.2、§15.2）：幂等、服务端截止、持久回执。

关键顺序：
1. 请求进入处理器即记录可信接收时间 received_at（不信任客户端时间）；
2. 在途计数登记（供回合关闭排空）；
3. 事务内复核会话/控制权/资格/回合/选项，唯一约束保证一人一回合一卷；
4. 事务提交后才返回回执，默认不含 is_correct。
"""
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_user, get_db
from app.core.errors import AppError, ErrorCode
from app.core.ratelimit import check_or_429
from app.core.security import fingerprint, new_receipt_id, token_hash
from app.db.base import as_utc, iso_z, utcnow
from app.db.models import (
    Answer,
    ControlLease,
    Match,
    MatchEntry,
    MatchQuestion,
    QuestionVersion,
    RoundInstance,
    User,
    ROUND_CLOSED,
    ROUND_OPEN,
)
from app.modules.competitions.service import inflight_begin, inflight_end

router = APIRouter(prefix="/api/v1", tags=["submissions"])


class AnswerIn(BaseModel):
    option_id: str = Field(min_length=1, max_length=16)


def _receipt(a: Answer) -> dict:
    return {"receipt_id": a.receipt_id, "round_instance_id": a.round_instance_id,
            "accepted_at": as_utc(a.received_at).isoformat().replace("+00:00", "Z"),
            "status": "accepted"}


@router.post("/matches/{match_id}/rounds/{round_instance_id}/answers")
async def submit_answer(
        match_id: str, round_instance_id: str, body: AnswerIn,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        control_token: str = Header(default="", alias="X-Control-Token"),
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)) -> dict:
    settings = get_settings()
    check_or_429(f"submit:{user.id}", settings.rate_limit_submit_per_min)
    if not 8 <= len(idempotency_key) <= 64:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "Idempotency-Key 长度须在 8–64。")

    received_at = utcnow()  # 可信接收时间（§10.2）
    inflight_begin(round_instance_id)
    try:
        # 答题控制权：仅当前活跃会话持有的租约有效（§16.1）
        lease = await db.get(ControlLease, user.id)
        if (lease is None or lease.token != token_hash(control_token)
                or lease.session_id != request.state.session_id):
            raise AppError(403, ErrorCode.CONTROL_TOKEN_INVALID,
                           "本设备/标签页未持有答题控制权，请刷新页面重新认领。")

        entry = (await db.execute(select(MatchEntry).where(
            MatchEntry.match_id == match_id, MatchEntry.user_id == user.id))).scalar_one_or_none()
        if entry is None:
            raise AppError(403, ErrorCode.NOT_ELIGIBLE, "你没有本场比赛的参赛记录。")
        # 先把参赛记录 id 落地成局部变量：下面的 IntegrityError 分支会 rollback，
        # 之后 `entry.id` 会因实例过期而触发隐式加载 → MissingGreenlet（500）。
        entry_id = entry.id

        round_ = await db.get(RoundInstance, round_instance_id)
        if round_ is None or round_.match_id != match_id:
            raise AppError(404, ErrorCode.NOT_FOUND, "回合不存在或不属于本场比赛。")
        # 接力赛：每道题只属于一个学院，别院不得代答（服务端强制，不靠前端隐藏）
        scope = getattr(round_, "house_scope", None)
        if scope and entry.house_key_snapshot not in scope:
            raise AppError(403, ErrorCode.NOT_YOUR_HOUSE,
                           "这道题不属于你的学院。接力赛中每个学院有自己的题目。",
                           {"scope": scope, "your_house": entry.house_key_snapshot})

        # 幂等预检：同键已持久化 → 回放原回执（即使已截止，§10.6）
        fp = fingerprint(match_id, round_instance_id, entry_id, body.option_id)
        existing = (await db.execute(select(Answer).where(
            Answer.match_id == match_id, Answer.entry_id == entry_id,
            Answer.idempotency_key == idempotency_key))).scalar_one_or_none()
        if existing is not None:
            if existing.fingerprint != fp:
                raise AppError(409, ErrorCode.IDEMPOTENCY_CONFLICT,
                               "同一幂等键对应了不同答案内容，已保留原答案。")
            return _receipt(existing)

        # 截止判定使用 received_at；事务稍晚完成不改变判定（§10.2）
        recv = as_utc(received_at)
        if round_.status == "void":
            raise AppError(409, ErrorCode.ROUND_CLOSED, "本回合已作废，不计分。")
        # 关回合不再睡眠排空（见 competitions/service.close_round 的说明）：
        # 状态可能已被控制器翻成 closed，但本请求的 received_at 在截止前就已到达，
        # 因此在宽限窗口内照常受理；只有 received_at 真正超过截止的才拒绝。
        grace_s = settings.answer_drain_budget_s
        base_ok = round_.status == ROUND_OPEN or round_.status == ROUND_CLOSED
        if not base_ok or round_.open_at is None or round_.deadline_at is None:
            raise AppError(409, ErrorCode.ROUND_NOT_OPEN, "当前回合未开放作答。")
        open_at, deadline = as_utc(round_.open_at), as_utc(round_.deadline_at)
        if recv < open_at:
            # 客户端时钟或快照有轻微提前：不是"通道关闭"，而是"还没开"。
            # 带上 open_at 让前端知道该重试而不是弹失败（控制器 tick 比 open_at 晚几秒
            # 的情况下，这是**唯一**会误伤正常提交的路径）。
            raise AppError(409, ErrorCode.ROUND_CLOSED, "作答通道尚未开放，请稍候重试。",
                           {"reason": "too_early", "open_at": iso_z(open_at)})
        if recv > deadline:
            raise AppError(409, ErrorCode.ROUND_CLOSED, "作答通道已关闭。",
                           {"reason": "too_late", "deadline_at": iso_z(deadline)})
        if round_.status == ROUND_CLOSED and (recv - deadline).total_seconds() > grace_s:
            # 理论上到达不了（received_at <= deadline），保留为不变量断言
            raise AppError(409, ErrorCode.ROUND_CLOSED, "作答通道已关闭。",
                           {"reason": "too_late", "deadline_at": iso_z(deadline)})
        # 抢答/接力的作答用时：从**这一回合真正对选手开放的时刻**算起。
        #
        # 不能用落库的 open_at：控制器迟到一个 tick 时，advance_match 会把 open_at 往前
        # 挪最多一个 tick，让"刚开放的瞬间提交"不被误判 too_early。那是防误伤的余量，
        # 不是真的提前开放；拿它当计时起点，每个人的用时都会被凭空多算 0.2–1 秒，
        # 综合评估模式下速度分立刻失真（分数要能追溯，就不能有这种系统性偏移）。
        #
        # display_at 是回合创建时写下的**原定开放时刻**，不受那层余量影响，因此用它计时。
        # 抢答模式下它同时也是题目出现的时刻（读题与作答同窗口）。
        start_ref = as_utc(round_.display_at)
        if start_ref is None or start_ref < open_at:
            # 理论上不会发生（display_at 总是早于或等于 open_at）；
            # 一旦数据异常就以更晚者为准，保证用时非负、不出现"负数用时"这种脏数据。
            start_ref = open_at
        elapsed_ms = max(0, int((recv - start_ref).total_seconds() * 1000))
        # 选项必须属于本回合冻结的题目版本（内部 option_id，§4）
        mq = await db.get(MatchQuestion, round_.match_question_id)
        v = await db.get(QuestionVersion, mq.version_id)
        if body.option_id not in {o["option_id"] for o in v.options_json}:
            raise AppError(422, ErrorCode.INVALID_OPTION, "选项不属于本题。")

        answer = Answer(match_id=match_id, round_instance_id=round_instance_id,
                        entry_id=entry_id, option_id=body.option_id,
                        idempotency_key=idempotency_key, received_at=received_at,
                        receipt_id=new_receipt_id(), fingerprint=fp,
                        elapsed_ms=elapsed_ms)
        db.add(answer)
        try:
            async with db.begin_nested():
                await db.flush()
        except IntegrityError:
            # 并发竞争：唯一约束只放行一份有效答案；同内容回放原回执，异内容拒绝（§10.2）
            #
            # ⚠️ 这里必须**显式 rollback**，不能只靠 begin_nested 回滚 savepoint。
            # 实测（并发 8 次同幂等键提交）：7 次走到这一行后，紧接着的 SELECT 抛
            #   PendingRollbackError: This Session's transaction has been rolled back
            #   due to a previous exception during flush ...
            # → 500 INTERNAL_ERROR。选手端看到"结果未知 · 恢复中"，
            # 而按设计这时**应该**回放原回执（§5 应急表就是这么承诺的）。
            # 显式 rollback 让会话回到可用状态；本请求只有这一条写入，回滚不会丢别的东西。
            #
            # 注意：rollback 会把 entry / round_ 这些 ORM 实例标记为过期，
            # 所以下面只能用**新查出来的 winner**，不能再碰它们（那是 MissingGreenlet 的来源）。
            await db.rollback()
            winner = (await db.execute(select(Answer).where(
                Answer.round_instance_id == round_instance_id,
                Answer.entry_id == entry_id))).scalar_one_or_none()
            if winner is not None and winner.fingerprint == fp:
                return _receipt(winner)
            raise AppError(409, ErrorCode.ANSWER_ALREADY_LOCKED,
                           "本回合已存在一份不同内容的有效答案。")
        await db.commit()
        return _receipt(answer)
    finally:
        inflight_end(round_instance_id)
