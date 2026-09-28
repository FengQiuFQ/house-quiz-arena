"""练习（B2 重写）。

与旧版的区别：
- **不需要登录**。匿名访客经 `mgus_guest` Cookie 获得身份，可练习、可上榜（显示「枫禾学子 #nn」）。
- 题目从**练习池**抽取；练习池可用性与正式池审核红线相互独立（见 _PRACTICE_USABLE 注释）。
- 服务端记录每题用时（`elapsed_ms`）与整场用时，用于速度榜与题目难度分析。
- 一场答完即结算：正确数、正确率、最长连对、总用时 → 写回会话并在达到门槛时标记入榜。
  结算只做一次（`is_ranked` 或 `status=finished` 后不再重算）。

练习成绩与正式赛成绩**物理分区**：本模块只写 practice_* 表，永不触碰 matches/answers。
"""
from __future__ import annotations

import random
import time
from typing import Iterable

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_db, get_current_user
from app.core.errors import AppError, ErrorCode
from app.core.ratelimit import check_or_429
from app.core.security import new_id
from app.db.base import utcnow
from app.db.models import (
    POOL_PRACTICE,
    GuestIdentity,
    PracticeAnswer,
    PracticeSession,
    Question,
    QuestionVersion,
    User,
)
from app.modules.practice.identity import (
    ANONYMOUS_NAME,
    ensure_guest,
    resolve_guest,
)
from app.modules.practice.leaderboard import MIN_RANKED, display_name_of

router = APIRouter(prefix="/api/v1", tags=["practice"])

# 单题用时下限：低于此值不计入速度统计（防连点刷榜），但**仍计入正确率**
MIN_PLAUSIBLE_MS = 400

# 练习池可用性：正式赛池必须 published + approved（§6 红线，不变）；
# 练习池用于自主刷题与排行榜，种子/导入的练习题（draft/pending）应当立刻可练。
# 练习池**永不**参与正式抽题（competitions._resolve_questions 只查 pool=official）。
_PRACTICE_USABLE = (Question.pool == POOL_PRACTICE) & (Question.status != "retired")

# 一次练习最多能勾几个主题。取 11 是因为题库就是 11 个主题：
# 超过它必然是把别的字段塞进了 subjects（或前端漏了去重），
# 明确报错比"悄悄忽略多出来的几个"好 —— 后者会让人以为勾上了却没生效。
MAX_PRACTICE_SUBJECTS = 11


def _normalize_subjects(raw: list[str] | None) -> list[str]:
    """主题多选入参清洗：去首尾空白、丢掉空串、按首次出现的顺序去重。

    为什么要去重：`in_()` 里重复项不影响抽题，但回显给前端的 subjects 会变成
    「科学常识, 科学常识」，前端照着算"已选 2 个主题"就是假的。
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = (item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


class LearnerCtx:
    """一次请求的学习者身份：登录用户或匿名访客（至少其一）。"""

    def __init__(self, user: User | None, guest) -> None:
        self.user = user
        self.guest = guest

    @property
    def key(self) -> str:
        """限流键。登录用 user_id；匿名用访客行 id（不含令牌明文）。"""
        return f"user:{self.user.id}" if self.user else f"guest:{self.guest.id}"

    @property
    def house_id(self) -> str | None:
        return self.user.house_id if self.user else None


async def learner_ctx(request: Request, response: Response,
                      db: AsyncSession = Depends(get_db)) -> LearnerCtx:
    """解析学习者身份。使用 get_optional_user 的语义：先看会话，再看访客 Cookie。"""
    try:
        from app.core.deps import get_current_user as _gcu
        user = await _gcu(request, db)
    except AppError:
        user = None
    if user is not None:
        return LearnerCtx(user, None)
    guest = await ensure_guest(request, response, db)
    return LearnerCtx(None, guest)


def _question_out(v: QuestionVersion, q: Question | None = None) -> dict:
    return {
        "question_version_id": v.id,
        "subject": q.subject if q is not None else None,
        "difficulty": q.difficulty if q is not None else None,
        "prompt": v.prompt,
        "options": [{"option_id": o["option_id"], "text": o["text"]} for o in v.options_json],
    }


@router.get("/practice/subjects")
async def subjects(db: AsyncSession = Depends(get_db)) -> dict:
    """可选主题与题量。公开，无需登录。"""
    rows = (await db.execute(
        select(Question.subject, func.count()).join(
            QuestionVersion, QuestionVersion.id == Question.current_version_id)
        .where(_PRACTICE_USABLE)
        .group_by(Question.subject)
    )).all()
    total = (await db.execute(
        select(func.count()).select_from(Question).where(_PRACTICE_USABLE))).scalar_one()
    # published 与 total 用**同一个** _PRACTICE_USABLE 基数，只多一个状态条件：
    # 两个数必须可比较（同一批题里有多少道过了审核），否则会出现 published > total。
    #
    # 为什么单列一个数：练习池的规则确实是"非 retired 就能练"（含 draft/pending），
    # total 没错；但界面上只写"题库共 178 题"会让人以为这 178 道都审核过。
    published = (await db.execute(
        select(func.count()).select_from(Question)
        .where(_PRACTICE_USABLE, Question.status == "published"))).scalar_one()
    return {"items": [{"subject": s, "count": c} for s, c in rows],
            "total": int(total), "published": int(published)}


class SessionIn(BaseModel):
    subject: str | None = None
    # 多主题（本次新增）。与 subject 并存：旧调用方仍在用单主题，不能删。
    subjects: list[str] | None = None
    count: int = Field(default=10, ge=1, le=50)
    difficulty: int | None = Field(default=None, ge=1, le=3)


@router.post("/practice/sessions")
async def create_session(body: SessionIn, request: Request, response: Response,
                         ctx: LearnerCtx = Depends(learner_ctx),
                         db: AsyncSession = Depends(get_db)) -> dict:
    check_or_429(f"practice:new:{ctx.key}", get_settings().rate_limit_practice_per_min)
    picked_subjects = _normalize_subjects(body.subjects)
    if len(picked_subjects) > MAX_PRACTICE_SUBJECTS:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"一次最多选择 {MAX_PRACTICE_SUBJECTS} 个主题。")
    # 旧调用方（单主题）走 subject 字段。它与"勾一个主题"是同一件事，
    # 所以先归一到同一个 scope 再往下走 —— 两条路各写一份过滤/落库逻辑迟早走样。
    # 这里刻意**不做** strip：既有行为就是拿原串等值过滤，改了会变成"以前查不到、
    # 现在能查到"，属于顺带修语义，不在本次范围内。
    legacy_subject = body.subject or None
    scope = picked_subjects or ([legacy_subject] if legacy_subject else [])

    stmt = (select(QuestionVersion)
            .join(Question, Question.current_version_id == QuestionVersion.id)
            .where(_PRACTICE_USABLE))
    # 主题过滤优先级：多选 > 单选 > 不限。
    # `subjects` 清洗后为空（含显式传 []）＝"全部主题"，与一个都不传等价，
    # 这样前端不必区分"没选"和"清空了选择"两种情况。
    if picked_subjects:
        stmt = stmt.where(Question.subject.in_(picked_subjects))
    elif legacy_subject:
        stmt = stmt.where(Question.subject == legacy_subject)
    if body.difficulty:
        stmt = stmt.where(Question.difficulty == body.difficulty)
    ids = [vid for (vid,) in (await db.execute(
        stmt.with_only_columns(QuestionVersion.id))).all()]
    if not ids:
        raise AppError(404, ErrorCode.PRACTICE_ERROR, "练习池暂无可用题目。")
    random.shuffle(ids)
    chosen = ids[: body.count]

    # 会话表的 subject 是 String(32) 单值列，放不下多个主题名；
    # 为多主题加列/改类型要动数据模型与迁移，本次刻意不做。于是：
    #   · 只勾 1 个主题（含旧调用方的单主题）→ 照旧存该主题名，
    #     GET 的 subject 字段语义完全不变；
    #   · 勾多个 → 存 None，真实范围由响应里的 subjects 交代。
    stored_subject = scope[0] if len(scope) == 1 else None

    sess = PracticeSession(
        id=new_id(),
        user_id=ctx.user.id if ctx.user else None,
        guest_id=ctx.guest.id if ctx.guest else None,
        house_id=ctx.house_id,
        subject=stored_subject,
        question_ids=chosen,
        # 登录用户可自愿公开昵称；未设置则上榜显示「枫禾学子」
        display_name=(ctx.user.display_name if ctx.user else None),
    )
    db.add(sess)
    await db.commit()

    versions = (await db.execute(
        select(QuestionVersion).where(QuestionVersion.id.in_(chosen))
    )).scalars().all()
    # 注意：questions 必须以 **question_version_id** 为键。
    # 曾经误用 q.id（题目 id）作键、却用 vid（版本 id）去查，结果主题与难度恒为 null。
    questions = {q.current_version_id: q for q in (await db.execute(
        select(Question).where(Question.current_version_id.in_(chosen))
    )).scalars()}
    by_vid = {v.id: v for v in versions}
    return {
        "session_id": sess.id,
        "learner": display_name_of(sess, ctx.guest),
        "anonymous": ctx.user is None,
        "subjects": _session_subjects(
            scope,
            (q.subject for q in (questions.get(vid) for vid in chosen) if q is not None)),
        "questions": [_question_out(by_vid[vid], questions.get(vid)) for vid in chosen if vid in by_vid],
        "asked_at": utcnow(),
    }


def _session_subjects(picked: list[str], drawn: Iterable[str | None]) -> list[str]:
    """一场练习"涉及哪些主题"。

    · 勾了主题 → 就是勾的那几个：范围在抽题前就定死了，直接回显，不受随机结果影响。
      反过来做（按抽到的题反推）会有假象：勾了 A+B 却恰好连抽到 5 道 A 时，
      界面就会说"这场只练了 A"。
    · 没勾（全部主题）→ 由**真正抽到的题**反推。此时"范围"是全部 11 个主题，
      说出来没有信息量；说清实际抽到哪几个才对使用者有用。
    """
    if picked:
        return list(picked)
    return sorted({name for name in drawn if name})


def _owns(sess: PracticeSession, ctx: LearnerCtx) -> bool:
    if ctx.user is not None:
        return sess.user_id == ctx.user.id
    return sess.guest_id is not None and sess.guest_id == ctx.guest.id


@router.get("/practice/sessions/{session_id}")
async def get_session(session_id: str, request: Request, response: Response,
                      ctx: LearnerCtx = Depends(learner_ctx),
                      db: AsyncSession = Depends(get_db)) -> dict:
    sess = await db.get(PracticeSession, session_id)
    if sess is None or not _owns(sess, ctx):
        raise AppError(404, ErrorCode.NOT_FOUND, "练习会话不存在。")
    versions = (await db.execute(
        select(QuestionVersion).where(QuestionVersion.id.in_(sess.question_ids))
    )).scalars().all()
    by_id = {v.id: v for v in versions}
    answered = {a.question_version_id: a for a in (await db.execute(
        select(PracticeAnswer).where(PracticeAnswer.session_id == session_id)
    )).scalars()}
    # 匿名会话的展示名要带访客序号（见 leaderboard.display_name_of 的说明）：
    # 这里按会话自己的 guest_id 取，而不是按当前请求的 ctx.guest —— 换浏览器打开旧会话时，
    # 展示的仍应是这份成绩当时的那个序号。
    sess_guest = await db.get(GuestIdentity, sess.guest_id) if sess.guest_id else None
    # 会话里只落了单个主题（见 create_session 的说明），所以多主题/不限主题要按题面反推 ——
    # 得出来的必然是**实际出到的**主题，可能比当初勾的范围窄（随机抽题抽偏了）。
    # 已落单主题时不查库：那条路径（旧调用方与单选）不需要反推。
    if sess.subject:
        sess_subjects = [sess.subject]
    else:
        sess_subjects = _session_subjects([], (await db.execute(
            select(Question.subject).where(Question.current_version_id.in_(sess.question_ids))
        )).scalars())
    return {
        "session_id": sess.id,
        "status": sess.status,
        "learner": display_name_of(sess, sess_guest),
        "anonymous": sess.user_id is None,
        "subject": sess.subject,
        "subjects": sess_subjects,
        "questions": [_question_out(by_id[vid]) for vid in sess.question_ids if vid in by_id],
        "answers": [{"question_version_id": qv, "option_id": a.option_id,
                     "is_correct": a.is_correct, "elapsed_ms": a.elapsed_ms}
                    for qv, a in answered.items()],
        "summary": _summary(sess),
    }


def _summary(sess: PracticeSession) -> dict:
    return {
        "answered_count": sess.answered_count,
        "correct_count": sess.correct_count,
        "accuracy": (round(100.0 * sess.correct_count / sess.answered_count, 2)
                     if sess.answered_count else 0.0),
        "best_streak": sess.best_streak,
        "total_ms": sess.total_ms,
        "ranked": sess.is_ranked,
        "ranked_threshold": MIN_RANKED,
        "note": (f"累计作答 ≥ {MIN_RANKED} 题才进入排行榜；"
                 "练习成绩不计入 House 官方积分。"),
    }


class AnswerIn(BaseModel):
    question_version_id: str
    option_id: str = Field(max_length=16)
    # 客户端上报的用时**仅用于异常检测**，服务端一律用自己的受理时间计算 elapsed_ms
    client_elapsed_ms: int | None = Field(default=None, ge=0, le=3_600_000)


@router.post("/practice/sessions/{session_id}/answers")
async def submit_answer(session_id: str, body: AnswerIn, request: Request, response: Response,
                        ctx: LearnerCtx = Depends(learner_ctx),
                        db: AsyncSession = Depends(get_db)) -> dict:
    received_at = utcnow()
    check_or_429(f"practice:ans:{ctx.key}", get_settings().rate_limit_practice_per_min)
    sess = await db.get(PracticeSession, session_id)
    if sess is None or not _owns(sess, ctx):
        raise AppError(404, ErrorCode.NOT_FOUND, "练习会话不存在。")
    if body.question_version_id not in sess.question_ids:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "该题不属于本练习会话。")

    existing = (await db.execute(select(PracticeAnswer).where(
        PracticeAnswer.session_id == session_id,
        PracticeAnswer.question_version_id == body.question_version_id))).scalar_one_or_none()
    if existing is not None:
        # 重复提交返回原结果（幂等），不重复计时、不重复计数
        v0 = await db.get(QuestionVersion, body.question_version_id)
        return {"is_correct": existing.is_correct, "already_answered": True,
                "correct_option_id": v0.correct_option_id if v0 else None,
                "explanation": v0.explanation if v0 else None,
                "elapsed_ms": existing.elapsed_ms,
                "summary": _summary(sess)}

    v = await db.get(QuestionVersion, body.question_version_id)
    if v is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "题目版本不存在。")
    if body.option_id not in {o["option_id"] for o in v.options_json}:
        raise AppError(422, ErrorCode.INVALID_OPTION, "无效选项。")
    is_correct = body.option_id == v.correct_option_id

    # 服务端用时：本会话上一次作答（或建会话）到本次受理
    prev = (await db.execute(
        select(func.max(PracticeAnswer.id)).where(PracticeAnswer.session_id == session_id)
    )).scalar_one_or_none()
    if prev is None:
        started = sess.created_at
    else:
        row = await db.get(PracticeAnswer, prev)
        started = row.answered_at if row is not None else sess.created_at
    from app.db.base import as_utc
    elapsed_ms = max(0, int((as_utc(received_at) - as_utc(started)).total_seconds() * 1000))
    # 单题低于 0.4 秒：仍计入正确率，但不计入速度统计（防连点）
    slow_enough = elapsed_ms >= MIN_PLAUSIBLE_MS

    db.add(PracticeAnswer(session_id=session_id, question_version_id=body.question_version_id,
                          option_id=body.option_id, is_correct=is_correct,
                          answered_at=received_at,
                          elapsed_ms=elapsed_ms if slow_enough else None))
    sess.cursor = min(sess.cursor + 1, len(sess.question_ids))
    sess.answered_count += 1
    if is_correct:
        sess.correct_count += 1

    total = (await db.execute(select(func.count()).select_from(PracticeAnswer)
                              .where(PracticeAnswer.session_id == session_id))).scalar_one()
    if total >= len(sess.question_ids):
        sess.status = "finished"
        sess.finished_at = received_at
        await _finalize(db, sess)
    await db.commit()
    return {
        "is_correct": is_correct,
        "correct_option_id": v.correct_option_id,
        "explanation": v.explanation,
        "elapsed_ms": elapsed_ms,
        "counted_for_speed": slow_enough,
        "summary": _summary(sess),
    }


async def _finalize(db: AsyncSession, sess: PracticeSession) -> None:
    """一场练习结束后的结算：总用时、最长连对、入榜标记。

    只结算一次——重复调用不改变已有结果（避免刷接口改成绩）。
    """
    if sess.is_ranked:
        return
    answers = (await db.execute(
        select(PracticeAnswer).where(PracticeAnswer.session_id == sess.id)
        .order_by(PracticeAnswer.answered_at, PracticeAnswer.id)
    )).scalars().all()
    if not answers:
        return

    longest = cur = 0
    for a in answers:
        cur = cur + 1 if a.is_correct else 0
        longest = max(longest, cur)
    sess.best_streak = longest

    times = [a.elapsed_ms for a in answers if a.elapsed_ms]
    sess.total_ms = int(sum(times)) if times else None
    sess.answered_count = len(answers)
    sess.correct_count = sum(1 for a in answers if a.is_correct)
    # 精确分数：正确率 ×10000 存整数分子，避免浮点比较
    sess.score_num = sess.correct_count * 10000
    sess.score_den = max(1, sess.answered_count)
    if sess.answered_count >= MIN_RANKED:
        sess.is_ranked = True
