"""比赛服务层（§4、§10.2、§10.3、§15.1）。

- 建赛抽题（冻结版本 + 封存备用题）、签到、开赛冻结名单
- 回合生命周期：开始（展示→开放→截止）、关闭（排空在途）、作废（备用题顶替 + 援助退回）
- 管理员命令：command_id 幂等 + expected_state_version 乐观控制，事务内完成
  状态变更 / 版本递增 / 事件与审计记录；重复请求回放原结果。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.security import new_id
from app.db.base import as_utc, utcnow
from app.db.models import (
    AdminCommand,
    Answer,
    AidRequest,
    House,
    Match,
    MatchEntry,
    MatchEvent,
    MatchHouseState,
    MatchQuestion,
    Question,
    QuestionVersion,
    ResultVersion,
    RoundInstance,
    User,
    MATCH_ARCHIVED,
    MATCH_CALCULATED,
    MATCH_DRAFT,
    MATCH_LOBBY,
    MATCH_LOCKED,
    MATCH_PAUSED,
    MATCH_REVEALED,
    MATCH_REVEAL_SCHEDULED,
    MATCH_RUNNING,
    MODE_FIXED_K,
    MODE_OPEN,
    QUESTION_SOURCES,
    ROUND_CLOSED,
    ROUND_DISPLAYING,
    ROUND_OPEN,
    ROUND_PENDING,
    SPECTATE_MIRROR,
    ROUND_VOID,
    SCORE_AVG,
    SCORE_MODE_COMPOSITE,
    SCORE_MODE_PURE,
    SCORE_TOP_N,
    SCORING_METHODS,
    SCORING_MODES,
    COMPETITION_MODES,
    COMP_MODE_BUZZ,
    COMP_MODE_RELAY,
    ACTIVE_HOUSE_KEYS,
    DEFAULT_WEIGHT_ACC,
    DEFAULT_WEIGHT_PARTICIPATION,
    DEFAULT_WEIGHT_SPEED,
    DEFAULT_WEIGHT_TIME,
    SRC_BLUEPRINT,
    SRC_CUSTOM,
    SRC_RANDOM,
    POOL_OFFICIAL,
    POOL_PRACTICE,
)
from app.modules.audit.service import audit
from app.modules.competitions import rules, statemachine as sm
from app.modules.scoring.service import calculate_result, publish_reveal
from app.realtime.hub import hub
from app.realtime import protocol

log = logging.getLogger("mgus.competitions")

# 回合在途请求计数（§10.2 排空；进程内，单 worker）
_inflight: dict[str, int] = {}


def inflight_begin(round_id: str) -> None:
    _inflight[round_id] = _inflight.get(round_id, 0) + 1


def inflight_end(round_id: str) -> None:
    _inflight[round_id] = max(0, _inflight.get(round_id, 0) - 1)


async def _emit(db: AsyncSession, match: Match, ev_type: str, payload: dict,
                broadcasts: list[dict]) -> None:
    db.add(MatchEvent(match_id=match.id, state_version=match.state_version,
                      type=ev_type, payload=payload))
    broadcasts.append({"type": ev_type, "match_id": match.id, "payload": payload,
                       "state_version": match.state_version})


# ── 建赛与抽题 ───────────────────────────────────────────────────────────────

# 两个题库：竞赛题库（official）与日常题库（practice）。
#
# 为什么要让建赛能选题库（而不再写死 official）：
#   日常题库里也有大量成色不错的通识题，办一场小型的、非正式的院内赛完全够用；
#   写死 official 会让这些题永远出不了场。
#
# ⚠️ 这里放开的是**建赛**选题库，不是练习抽竞赛题。练习仍然只从 practice 池抽题
#   （practice/router.py 的 _PRACTICE_USABLE），"日常刷题不会提前见到正式赛题"这条红线不变。
#   反方向是安全的：拿日常题去办赛，最坏结果是"学生练过这道题"，不会泄题。
#
# 两个池在**建赛时用同一套可用性门槛**（published + 当前版本 approved）：
#   正式赛的成绩是要公开揭晓、还要给别人复核的，拿一道没审过的题去定名次，
#   一旦答案键是错的，成绩就不可信了。日常池里那批 draft 题因此不进正式赛，
#   控制台的配额预览会把实际可用量摆出来，管理员开赛前就能看到够不够。
POOL_LABELS = {POOL_OFFICIAL: "竞赛题库", POOL_PRACTICE: "日常题库"}


def pool_label(pool: str) -> str:
    """题库的对外称呼。日志与错误提示里一律用它，不再出现 official/practice 这种内部键。"""
    return POOL_LABELS.get(pool, pool)


def normalize_pool(raw: Any) -> str:
    """校验建赛时传来的题库标识；缺省是竞赛题库。"""
    pool = (raw or POOL_OFFICIAL)
    if pool not in (POOL_OFFICIAL, POOL_PRACTICE):
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"question_pool 必须是 {'/'.join(POOL_LABELS)}。")
    return pool


def _usable_question_stmt(pool: str):
    """某个题库里"能进正式赛"的题：已发布 + 当前版本已审核通过。"""
    return (select(Question)
            .join(QuestionVersion, QuestionVersion.id == Question.current_version_id)
            .where(Question.pool == pool, Question.status == "published",
                   QuestionVersion.review_state == "approved"))


async def _resolve_questions(db: AsyncSession, planned: int,
                             question_ids: list[str] | None,
                             blueprint: dict[str, int] | None,
                             difficulty_mix: dict | None = None,
                             pool: str = POOL_OFFICIAL) -> list[Question]:
    """按题源从**所选题库的已审核题**里取题。三种题源共用同一套可用性校验。

    难度配比（difficulty_mix）在随机/配额模式下作为约束：{"1":n,"2":n,"3":n}。
    配额不足时**明确报错并告出可用的实际数量**，而不是悄悄少出题——
    现场开赛前才发现题不够是最糟的情况。
    """
    label = pool_label(pool)
    pool_rows = (await db.execute(_usable_question_stmt(pool))).scalars().all()

    if question_ids:
        by_id = {q.id: q for q in pool_rows}
        missing = [qid for qid in question_ids if qid not in by_id]
        if missing:
            raise AppError(422, ErrorCode.VALIDATION_ERROR,
                           f"以下题目不在「{label}」已审核的题中（或不存在）", {"missing": missing})
        chosen = [by_id[qid] for qid in question_ids]
        if len(chosen) != planned:
            raise AppError(422, ErrorCode.VALIDATION_ERROR,
                           f"自定义题目数量与计划不符：选 {len(chosen)} 题，计划 {planned} 题")
        return chosen

    import random

    if blueprint:
        chosen = []
        for subject, count in blueprint.items():
            cands = [q for q in pool_rows if q.subject == subject]
            if len(cands) < count:
                raise AppError(422, ErrorCode.VALIDATION_ERROR,
                               f"「{label}」中主题「{subject}」可用题不足：需 {count}，有 {len(cands)}")
            chosen.extend(random.sample(cands, count))
        random.shuffle(chosen)
        if len(chosen) != planned:
            raise AppError(422, ErrorCode.VALIDATION_ERROR,
                           f"主题配额合计 {len(chosen)} 题，与计划 {planned} 题不符")
        return chosen

    # 随机（可带难度配比）
    if difficulty_mix:
        chosen = []
        need: dict[int, int] = {}
        for k, v in difficulty_mix.items():
            try:
                lvl, cnt = int(k), int(v)
            except (TypeError, ValueError):
                raise AppError(422, ErrorCode.VALIDATION_ERROR,
                               f"难度配比格式错误：{k}:{v}（应为 难度:题数）")
            if cnt > 0:
                need[lvl] = need.get(lvl, 0) + cnt
        for lvl, cnt in need.items():
            cands = [q for q in pool_rows if q.difficulty == lvl]
            if len(cands) < cnt:
                raise AppError(
                    422, ErrorCode.VALIDATION_ERROR,
                    f"「{label}」中难度 D{lvl} 可用题不足：需 {cnt}，有 {len(cands)}",
                    {"available": {d: len([q for q in pool_rows if q.difficulty == d])
                                   for d in sorted({q.difficulty for q in pool_rows})}})
            chosen.extend(random.sample(cands, cnt))
        if len(chosen) != planned:
            raise AppError(422, ErrorCode.VALIDATION_ERROR,
                           f"难度配比合计 {len(chosen)} 题，与计划 {planned} 题不符")
        random.shuffle(chosen)
        return chosen

    if len(pool_rows) < planned:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"「{label}」已审核题不足：需 {planned}，有 {len(pool_rows)}")
    return random.sample(pool_rows, planned)


async def quota_preview(db: AsyncSession, planned: int,
                        difficulty_mix: dict | None = None,
                        blueprint: dict[str, int] | None = None,
                        pool: str = POOL_OFFICIAL) -> dict:
    """建赛前的**配额预览**：先告诉管理员"这样抽能不能出题"，再真正建赛。

    现场最怕的是"点了建赛才发现题不够"。这里把可用量、缺口与建议一次算清。
    题库可选（竞赛/日常），因此预览必须跟着所选题库走 —— 否则管理员切到日常题库后
    看到的仍是竞赛题库的家底，预览就成了误导。
    """
    label = pool_label(pool)
    pool_rows = (await db.execute(_usable_question_stmt(pool))).scalars().all()

    by_subject: dict[str, int] = {}
    by_difficulty: dict[int, int] = {}
    for q in pool_rows:
        by_subject[q.subject] = by_subject.get(q.subject, 0) + 1
        by_difficulty[q.difficulty] = by_difficulty.get(q.difficulty, 0) + 1

    problems: list[str] = []
    if difficulty_mix:
        for k, v in difficulty_mix.items():
            try:
                lvl, cnt = int(k), int(v)
            except (TypeError, ValueError):
                problems.append(f"难度配比格式错误：{k}:{v}")
                continue
            have = by_difficulty.get(lvl, 0)
            if cnt > have:
                problems.append(f"难度 D{lvl} 需 {cnt} 题，仅有 {have} 题")
    if blueprint:
        for subject, cnt in blueprint.items():
            have = by_subject.get(subject, 0)
            if cnt > have:
                problems.append(f"主题「{subject}」需 {cnt} 题，仅有 {have} 题")
    if not difficulty_mix and not blueprint and len(pool_rows) < planned:
        problems.append(f"「{label}」需 {planned} 题，仅有 {len(pool_rows)} 题")

    # 备用题按计划题数的 20%（向下不少于 2）
    backup_need = max(2, -(-planned // 5))
    return {
        "question_pool": pool,
        "question_pool_label": label,
        "available_total": len(pool_rows),
        "by_subject": [{"subject": s, "count": c} for s, c in sorted(by_subject.items())],
        "by_difficulty": [{"difficulty": d, "count": c} for d, c in sorted(by_difficulty.items())],
        "planned_questions": planned,
        "backup_need": backup_need,
        "backup_short": len(pool_rows) - planned < backup_need,
        "ok": not problems,
        "problems": problems,
    }


async def create_match(db: AsyncSession, admin: User, payload: dict) -> Match:
    s = get_settings()
    mode = payload.get("mode", MODE_FIXED_K)
    if mode not in (MODE_FIXED_K, MODE_OPEN):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "mode 必须是 fixed_k/open。")
    fixed_k = payload.get("fixed_k")
    if mode == MODE_FIXED_K and not fixed_k:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "固定席位赛必须提供 fixed_k。")

    # ── 赛制（双模式）：先定赛制，再由赛制决定"全场题量"怎么来 ──────────────
    comp_mode = payload.get("competition_mode") or COMP_MODE_BUZZ
    if comp_mode not in COMPETITION_MODES:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"competition_mode 必须是 {'/'.join(COMPETITION_MODES)}。")
    questions_per_house = payload.get("questions_per_house")
    if comp_mode == COMP_MODE_RELAY:
        if not questions_per_house:
            raise AppError(422, ErrorCode.VALIDATION_ERROR,
                           "接力赛必须给出每院题目数量（questions_per_house）。")
        # 接力赛全场题量 = 每院题量 × 参赛学院数（由赛制推导，不采信手填的 planned_questions）：
        # 现场填表时很容易忘改 planned_questions，两个数不一致会让某个学院没题可做。
        scope_keys = payload.get("houses_scope") or list(ACTIVE_HOUSE_KEYS)
        planned = int(questions_per_house) * len(scope_keys)
    else:
        questions_per_house = None
        planned = int(payload.get("planned_questions") or 20)
    if not 1 <= planned <= 200:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"本场题量为 {planned}，超出允许范围 1–200（接力赛请调整每院题量）。")

    # 题源：显式声明优于从参数猜测 —— 猜错了会静默按别的口径抽题
    question_ids = payload.get("question_ids")
    blueprint = payload.get("blueprint")
    difficulty_mix = payload.get("difficulty_mix")
    # 题库（大类）：竞赛题库 / 日常题库，缺省竞赛题库。见 POOL_LABELS 处的说明。
    question_pool = normalize_pool(payload.get("question_pool"))
    source = payload.get("question_source")
    if not source:
        source = (SRC_CUSTOM if question_ids
                  else SRC_BLUEPRINT if blueprint else SRC_RANDOM)
    if source not in QUESTION_SOURCES:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"question_source 必须是 {'/'.join(QUESTION_SOURCES)}。")
    if source == SRC_CUSTOM and not question_ids:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "自定义题源必须提供 question_ids。")
    if source == SRC_BLUEPRINT and not blueprint:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "主题配额题源必须提供 blueprint。")

    # 评分标准：四院共用同一套，赛前冻结
    method = payload.get("scoring_method") or SCORE_AVG
    if method not in SCORING_METHODS:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"scoring_method 必须是 {'/'.join(SCORING_METHODS)}。")
    top_n = int(payload.get("scoring_top_n") or 5)
    if method == SCORE_TOP_N and not 1 <= top_n <= 50:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "scoring_top_n 须在 1–50。")

    # 评分模式与权重：同样是**赛前冻结**的计分口径。
    # 权重全 0 会让综合评估退化（见 rules.composite_score），因此直接拒绝而不是悄悄退化。
    # 变量名带 _mode 后缀：本函数里 `mode` 已经用于 fixed_k/open，不能重名。
    scoring_mode_v = payload.get("scoring_mode") or SCORE_MODE_PURE
    if scoring_mode_v not in SCORING_MODES:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"scoring_mode 必须是 {'/'.join(SCORING_MODES)}。")
    w_acc = int(payload.get("weight_accuracy") if payload.get("weight_accuracy") is not None
                else DEFAULT_WEIGHT_ACC)
    w_speed = int(payload.get("weight_speed") if payload.get("weight_speed") is not None
                  else DEFAULT_WEIGHT_SPEED)
    w_part = int(payload.get("weight_participation")
                 if payload.get("weight_participation") is not None
                 else DEFAULT_WEIGHT_PARTICIPATION)
    if not (0 <= w_acc <= 1000 and 0 <= w_speed <= 1000 and 0 <= w_part <= 1000):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "权重须在 0–1000。")
    if scoring_mode_v == SCORE_MODE_COMPOSITE and w_acc + w_speed + w_part <= 0:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       "综合评估模式下必须至少给正确率、速度或参与度一个非零权重。")

    # 读题窗口：0 = 抢答（默认赛制）。负数无意义，拒绝。
    display_seconds = int(payload.get("display_seconds") if payload.get("display_seconds") is not None
                          else s.default_display_seconds)
    if display_seconds < 0:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "display_seconds 不能为负（0 表示抢答）。")

    # 接力赛的时间权重（与正确率权重合成；抢答赛用 weight_speed）
    w_time = int(payload.get("weight_time") if payload.get("weight_time") is not None
                 else DEFAULT_WEIGHT_TIME)
    if not 0 <= w_time <= 1000:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "weight_time 须在 0–1000。")

    chosen = await _resolve_questions(
        db, planned,
        question_ids if source == SRC_CUSTOM else None,
        blueprint if source == SRC_BLUEPRINT else None,
        difficulty_mix if source == SRC_RANDOM else None,
        pool=question_pool,
    )
    # 封存备用题：ceil(20%)，与主题不重复（§10.3）
    # 必须与主用题**同池**取：跨池取备用题会让"这场用的是日常题库"变得不成立，
    # 而且备用题是要顶替作废回合真正下发给选手的。
    backup_n = max(2, -(-planned // 5))
    stmt = (select(Question)
            .join(QuestionVersion, QuestionVersion.id == Question.current_version_id)
            .where(Question.pool == question_pool, Question.status == "published",
                   QuestionVersion.review_state == "approved",
                   Question.id.notin_([q.id for q in chosen]))
            .order_by(func.random()).limit(backup_n))
    backups = (await db.execute(stmt)).scalars().all()

    def _opt(key: str, default: int) -> int:
        v = payload.get(key)
        # 显式区分「未提供」与「提供 0」——题间过渡 0 秒是合法配置
        return int(v) if v is not None else default

    match = Match(
        id=new_id(), title=payload.get("title") or "未命名 House 赛",
        mode=mode, status=MATCH_DRAFT, state_version=1,
        planned_questions=planned, fixed_k=fixed_k,
        min_house_size=_opt("min_house_size", s.default_min_house_size),
        min_valid_questions=rules.min_valid_questions(planned, s.min_valid_question_ratio),
        display_seconds=display_seconds,
        answer_seconds=_opt("answer_seconds", s.default_answer_seconds),
        interlude_seconds=_opt("interlude_seconds", s.default_interlude_seconds),
        aid_enabled=bool(payload.get("aid_enabled", True)),
        aid_window_seconds=_opt("aid_window_seconds", s.default_aid_window_seconds),
        aid_quota_per_house=_opt("aid_quota_per_house", 1),
        question_source=source,
        scoring_method=method,
        scoring_top_n=top_n,
        scoring_mode=scoring_mode_v,
        weight_accuracy=w_acc,
        weight_speed=w_speed,
        weight_participation=w_part,
        competition_mode=comp_mode,
        questions_per_house=questions_per_house,
        weight_time=w_time,
        difficulty_mix=difficulty_mix or None,
        # 观战档位与参赛范围可以在建赛时一次设好（现场临时改还能用 PATCH /admin/matches/{id}）
        spectator_mode=payload.get("spectator_mode") or SPECTATE_MIRROR,
        houses_scope=(payload.get("houses_scope") or None),
        created_by=admin.id,
    )
    db.add(match)
    for i, q in enumerate(chosen, start=1):
        db.add(MatchQuestion(id=new_id(), match_id=match.id, question_id=q.id,
                             version_id=q.current_version_id, position=i, is_backup=False))
    for j, q in enumerate(backups, start=planned + 1):
        db.add(MatchQuestion(id=new_id(), match_id=match.id, question_id=q.id,
                             version_id=q.current_version_id, position=j, is_backup=True))
    await audit(db, admin.id, "match.create", "match", match.id,
                {"title": match.title, "mode": mode, "planned": planned, "backups": len(backups),
                 "question_source": source, "scoring_method": method, "scoring_top_n": top_n,
                 "scoring_mode": scoring_mode_v, "weight_accuracy": w_acc, "weight_speed": w_speed,
                 "weight_participation": w_part,
                 "buzz": display_seconds == 0,
                 "difficulty_mix": difficulty_mix})
    await db.commit()
    return match


# ── 签到与名单 ───────────────────────────────────────────────────────────────


async def check_in(db: AsyncSession, user: User, match_id: str) -> dict:
    """签到入场。

    **谁能参赛：绑定了学院的账号都能，按自己学院上场。**
    这里曾经有两道额外的门：角色必须是 `entrant`，且名单里必须由管理员预先加一条资格记录。
    结果是学生注册完发现"我进不去比赛"，还要等管理员再点一次；管理员自己也被卷进参赛语义。
    现在两道门都去掉了 —— 学院绑定就是唯一条件，`house_scope` 负责"本场限哪几个学院"，
    角色只在**额外能力**上起作用（管理员管后台、智囊发起求援），不再决定能不能上场。
    """
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    if match.status != MATCH_LOBBY:
        raise AppError(409, ErrorCode.MATCH_NOT_JOINABLE, "当前不在签到阶段。")
    if user.house_id is None:
        # 管理员也一样：参赛按学院，不按角色。没绑定学院的账号上场不会给任何学院加分，
        # 学生只会觉得系统坏了 —— 所以这里和 /snapshot 用同一个错误码与提示。
        raise AppError(403, ErrorCode.HOUSE_REQUIRED,
                       "你的账号还没有归属学院，无法参赛。请联系管理员为你分配学院。",
                       {"needs_house": True})
    house = await db.get(House, user.house_id)
    if house is None:
        raise AppError(403, ErrorCode.NOT_ELIGIBLE, "账号绑定的学院不存在，请联系管理员。")

    # 只能加入**包含自己学院**的比赛（两院/限定范围赛时这条才起作用）
    scope = match.houses_scope or None
    if scope and house.key not in scope:
        names = []
        hs = {h.key: h.name_cn for h in (await db.execute(select(House))).scalars()}
        names = [hs.get(k, k) for k in scope]
        raise AppError(403, ErrorCode.NOT_YOUR_HOUSE,
                       f"本场只限 {'、'.join(names)} 参加，你所在的学院不在其中。",
                       {"scope": scope, "your_house": house.key})

    entry = (await db.execute(select(MatchEntry).where(
        MatchEntry.match_id == match_id, MatchEntry.user_id == user.id))).scalar_one_or_none()
    if entry is None:
        entry = MatchEntry(id=new_id(), match_id=match_id, user_id=user.id,
                           house_id=house.id, house_key_snapshot=house.key,
                           house_name_snapshot=house.name_cn, display_alias="")
        db.add(entry)
    entry.checked_in_at = utcnow()
    await db.commit()
    return {"checked_in": True, "match_id": match_id,
            "house_key": house.key, "house_name": house.name_cn}


async def set_captain(db: AsyncSession, admin: User, match_id: str, username: str) -> dict:
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    u = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if u is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "用户不存在。")
    entry = (await db.execute(select(MatchEntry).where(
        MatchEntry.match_id == match_id, MatchEntry.user_id == u.id))).scalar_one_or_none()
    if entry is None:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "该用户尚未签到进入本场名单。")
    # 每 House 至多一名队长
    others = (await db.execute(select(MatchEntry).where(
        MatchEntry.match_id == match_id, MatchEntry.house_id == entry.house_id,
        MatchEntry.is_captain.is_(True)))).scalars().all()
    for o in others:
        o.is_captain = False
    entry.is_captain = True
    await audit(db, admin.id, "match.set_captain", "match", match_id, {"username": username})
    await db.commit()
    return {"captain": username, "house": entry.house_key_snapshot}


async def _freeze_roster(db: AsyncSession, match: Match, allow_incomplete: bool) -> dict:
    entries = (await db.execute(select(MatchEntry).where(
        MatchEntry.match_id == match.id, MatchEntry.checked_in_at.is_not(None)))).scalars().all()
    by_house: dict[str, list[MatchEntry]] = {}
    for e in entries:
        by_house.setdefault(e.house_id, []).append(e)
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}

    if match.mode == MODE_FIXED_K:
        bad = {houses[hid].key: len(v) for hid, v in by_house.items()
               if len(v) != match.fixed_k}
        all_four = {hid: len(by_house.get(hid, [])) for hid in houses}
        if any(c != match.fixed_k for c in all_four.values()):
            if not allow_incomplete:
                raise AppError(409, ErrorCode.VALIDATION_ERROR,
                               "固定席位赛各 House 人数必须等于 K；确认要带缺口开赛请传 allow_incomplete=true（成绩将标记非正式）。",
                               {"counts": {houses[hid].key: c for hid, c in all_four.items()}})
    for hid, members in by_house.items():
        members.sort(key=lambda e: e.created_at)
        house = houses[hid]
        for i, e in enumerate(members, start=1):
            e.display_alias = f"{house.key}-{i:02d}"

    for hid in houses:
        db.add(MatchHouseState(id=new_id(), match_id=match.id, house_id=hid,
                               aid_remaining=match.aid_quota_per_house if match.aid_enabled else 0))
    return {"frozen": len(entries),
            "counts": {houses[hid].key: len(v) for hid, v in by_house.items()}}


# ── 回合生命周期 ─────────────────────────────────────────────────────────────


async def start_round(db: AsyncSession, match: Match, mq: MatchQuestion,
                      now: datetime, house_scope: list[str] | None = None) -> RoundInstance:
    """开一个回合。

    **抢答（buzz）**：`display_seconds == 0` 时题目一出现即可作答，读题与作答是同一段时间，
    没有"只能看不能答"的窗口。本系统的正式赛制就是抢答——知识竞赛的张力来自
    "谁先想到"，而不是"大家先一起看 N 秒"。
    `display_seconds > 0` 仍然保留：练习性质或需要主持人念题的场次可以用它。

    **接力（relay）**：`house_scope` 指定本回合只有哪个学院作答（一轮各院做不同的题）。
    回合仍然是全局的（同一时刻各院各有一道自己的题在开放），只是每道题归属一个学院；
    这样"每个 House 固定题目数量"与"总用时可比"两件事同时成立。
    """
    n = (await db.execute(select(func.count()).select_from(RoundInstance)
                          .where(RoundInstance.match_question_id == mq.id))).scalar_one()
    buzz = int(match.display_seconds or 0) <= 0
    display_at = now
    open_at = now if buzz else now + timedelta(seconds=match.display_seconds)
    deadline = open_at + timedelta(seconds=match.answer_seconds)
    r = RoundInstance(id=new_id(), match_id=match.id, match_question_id=mq.id,
                      attempt_no=n + 1,
                      # 抢答回合直接就是 open：不要先落一个 displaying 再等下一次 tick 翻状态，
                      # 那会让"下一题"多出最多一个 tick 的空白（现场看是"卡了一下"）。
                      status=ROUND_OPEN if buzz else ROUND_DISPLAYING,
                      house_scope=house_scope,
                      display_at=display_at.replace(tzinfo=None),
                      open_at=open_at.replace(tzinfo=None),
                      deadline_at=deadline.replace(tzinfo=None))
    db.add(r)
    match.current_round_id = r.id
    return r


async def start_relay_rounds(db: AsyncSession, match: Match, now: datetime) -> list[RoundInstance]:
    """接力赛开赛：一次把**每个学院自己的那道题**全部放出去（并行推进）。

    为什么不是"一回合一道题、各院轮流"：接力比的是"做完全部题目的总用时"，
    如果题目是全局串行的，慢的学院会拖住快的学院，总用时就不再是各院自己的成绩。
    所以接力的回合必须**按院并行**：同一时刻四院各自有一道自己的题开放，
    谁先做完自己那份题谁就快 —— 这正是"接力"的含义。

    题量：每个 House `questions_per_house` 道，四个学院合计消耗
    `questions_per_house × 学院数` 道题（题库按院均分）。
    """
    houses = (await db.execute(
        select(House).where(House.is_active.is_(True)).order_by(House.created_at)
    )).scalars().all()
    scope = match.houses_scope or [h.key for h in houses]
    active = [h for h in houses if h.key in scope]
    if not active:
        raise AppError(409, ErrorCode.INVALID_TRANSITION, "本场没有在册学院，无法开赛。")

    per_house = int(match.questions_per_house or 0)
    mains = (await db.execute(
        select(MatchQuestion).where(MatchQuestion.match_id == match.id,
                                    MatchQuestion.is_backup.is_(False))
        .order_by(MatchQuestion.position))).scalars().all()
    need = per_house * len(active)
    if len(mains) < need:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"接力赛需要 {need} 道题（{len(active)} 院 × 每院 {per_house} 道），"
                       f"本场仅冻结了 {len(mains)} 道。请在建赛时把题量调到至少 {need}。")

    rounds: list[RoundInstance] = []
    # 轮流分配而不是"前 N 道给第一院"：题目顺序往往按主题/难度排列，
    # 成块切分会让某一院拿到明显更难的一段，四院之间立刻不可比。
    for i in range(per_house):
        for j, h in enumerate(active):
            mq = mains[i * len(active) + j]
            rounds.append(await start_round(db, match, mq, now, house_scope=[h.key]))
    return rounds


async def close_round(db: AsyncSession, match: Match, r: RoundInstance,
                      now: datetime) -> None:
    """关闭回合：**不阻塞、不睡眠**（B1-E2）。

    原实现在事务里 `while _inflight: await asyncio.sleep(0.05)`，最长 3 秒；而本函数由
    `MatchController.tick_all()` 在**同一个 AsyncSession 事务内**对所有进行中的比赛串行调用——
    一场卡 3 秒，同事务内其他比赛的回合也会晚 3 秒，而 500 人最后一秒集体提交几乎必然触发它。

    正确性并不需要这个循环：截止判定用的是**请求进入处理器时记录的 `received_at`**
    （见 submissions/router.py），所以"截止前到达"的请求即使在状态翻成 closed 之后才走到
    状态校验，也仍应按 received_at 被受理——那份宽限逻辑放在提交端（drain_grace），
    这里只做纯状态变更。在途计数保留下来仅用于**监控告警**，不再用于阻塞。
    """
    pending_inflight = _inflight.get(r.id, 0)
    if pending_inflight > 0:
        budget_ms = int(get_settings().answer_drain_budget_s * 1000)
        log.warning("round closed with %s in-flight submissions round=%s", pending_inflight, r.id)
        await audit(db, None, "round.closed_with_inflight", "round", r.id,
                    {"inflight": pending_inflight, "grace_ms": budget_ms})
    r.status = ROUND_CLOSED
    r.closed_at = now.replace(tzinfo=None)
    if match.current_round_id == r.id:
        match.current_round_id = None
    # 求援超时按赛前规则消耗（不退回，§5）
    aids = (await db.execute(select(AidRequest).where(
        AidRequest.round_instance_id == r.id, AidRequest.status == "pending"))).scalars().all()
    for a in aids:
        a.status = "expired"


async def void_round(db: AsyncSession, match: Match, r: RoundInstance, reason: str) -> None:
    """作废回合：全场统一；援助额度退回并写明原因（§10.3）。"""
    r.status = ROUND_VOID
    r.void_reason = reason
    if match.current_round_id == r.id:
        match.current_round_id = None
    aids = (await db.execute(select(AidRequest).where(
        AidRequest.round_instance_id == r.id, AidRequest.status.in_(["pending", "answered"]))
    )).scalars().all()
    for a in aids:
        a.status = "refunded"
        st = (await db.execute(select(MatchHouseState).where(
            MatchHouseState.match_id == match.id,
            MatchHouseState.house_id == a.house_id))).scalar_one_or_none()
        if st is not None:
            st.aid_remaining += 1


async def _advance_relay(db: AsyncSession, match: Match, now: datetime,
                         broadcasts: list[dict]) -> list[dict]:
    """接力赛推进：题目**开赛时一次性放出**，这里只负责"到点收卷"。

    接力比的是每院做完全部题目的总用时，因此没有逐题推进、也没有"下一题"的概念：
    所有回合从头开到尾，谁先交完谁就快。到 `deadline_at` 统一收卷并锁定比赛。
    """
    rounds = (await db.execute(
        select(RoundInstance).where(RoundInstance.match_id == match.id)
    )).scalars().all()
    if not rounds:
        # 开赛后第一个 tick：把每院自己的那份题一次放出去
        await start_relay_rounds(db, match, now)
        broadcasts.append({"type": protocol.EV_ROUND_OPENED, "match_id": match.id,
                           "payload": {"relay": True}, "state_version": match.state_version})
        return broadcasts
    open_rounds = [r for r in rounds if r.status == ROUND_OPEN]
    if not open_rounds:
        return broadcasts
    if any(r.deadline_at is None or as_utc(now) < as_utc(r.deadline_at) for r in open_rounds):
        return broadcasts
    for r in open_rounds:
        await close_round(db, match, r, now)
        broadcasts.append({"type": protocol.EV_ROUND_CLOSED, "match_id": match.id,
                           "payload": {"round_id": r.id},
                           "state_version": match.state_version})
    match.status = MATCH_LOCKED
    match.state_version += 1
    await _emit(db, match, "match.auto_locked", {"reason": "relay_deadline"}, broadcasts)
    await audit(db, None, "match.auto_lock", "match", match.id, {"mode": "relay"})
    return broadcasts


async def close_all_open_rounds(db: AsyncSession, match: Match, now: datetime,
                                reason: str) -> list[str]:
    """收掉本场所有还开着的回合，返回回合 id 列表。

    为什么需要它：接力赛**同时有多个回合在开放**，而 `match.current_round_id` 只记得住
    最后一个。原来 lock/pause 只关 current_round_id，于是接力赛按下"结束作答"以后仍有
    十几个回合处于 open —— 结算时它们不算有效题，成绩直接算错。
    """
    rows = (await db.execute(
        select(RoundInstance).where(RoundInstance.match_id == match.id,
                                    RoundInstance.status.in_((ROUND_OPEN, ROUND_DISPLAYING)))
    )).scalars().all()
    out: list[str] = []
    for r in rows:
        if reason == "lock":
            await close_round(db, match, r, now)
        else:
            await void_round(db, match, r, reason)
        out.append(r.id)
    return out


async def pick_next_question(db: AsyncSession, match: Match) -> MatchQuestion | None:
    used = set((await db.execute(
        select(RoundInstance.match_question_id).where(RoundInstance.match_id == match.id)
    )).scalars())
    mains = (await db.execute(
        select(MatchQuestion).where(MatchQuestion.match_id == match.id, MatchQuestion.is_backup.is_(False))
        .order_by(MatchQuestion.position))).scalars().all()
    for mq in mains:
        if mq.id not in used:
            return mq
    # 主题耗尽且存在作废回合 → 用封存备用题顶替（§10.3 默认规则）
    has_void = (await db.execute(
        select(RoundInstance.id).where(RoundInstance.match_id == match.id,
                                       RoundInstance.status == ROUND_VOID).limit(1)
    )).scalar_one_or_none() is not None
    if has_void:
        backups = (await db.execute(
            select(MatchQuestion).where(MatchQuestion.match_id == match.id,
                                        MatchQuestion.is_backup.is_(True))
            .order_by(MatchQuestion.position))).scalars().all()
        for mq in backups:
            if mq.id not in used:
                return mq
    return None


async def advance_match(db: AsyncSession, match: Match, now: datetime | None = None) -> list[dict]:
    """控制器单步推进；返回需要广播的事件列表（提交后再广播）。

    now 可注入（测试/演示时间旅行）；生产传 None 取服务器当前时间。
    """
    now = now or utcnow()
    broadcasts: list[dict] = []

    if match.status == "reveal_scheduled":
        rv = await publish_reveal(db, match, as_utc(now))
        if rv is not None:
            broadcasts.append({"type": protocol.EV_RESULT_PUBLISHED, "match_id": match.id,
                               "payload": {"result_version_id": rv.id, "version_no": rv.version_no},
                               "state_version": match.state_version})
            broadcasts.append({"type": protocol.EV_MATCH_STATE, "match_id": match.id,
                               "payload": {"status": match.status},
                               "state_version": match.state_version})
            hub.reset_stream(match.id)
        return broadcasts

    if match.status != MATCH_RUNNING:
        return broadcasts

    if getattr(match, "competition_mode", COMP_MODE_BUZZ) == COMP_MODE_RELAY:
        return await _advance_relay(db, match, as_utc(now), broadcasts)

    r = None
    if match.current_round_id:
        r = await db.get(RoundInstance, match.current_round_id)

    if r is None:
        last_closed = (await db.execute(
            select(RoundInstance).where(RoundInstance.match_id == match.id,
                                        RoundInstance.status == ROUND_CLOSED)
            .order_by(RoundInstance.closed_at.desc()).limit(1))).scalar_one_or_none()
        if last_closed is not None and last_closed.closed_at is not None:
            next_ok = as_utc(last_closed.closed_at) + timedelta(seconds=match.interlude_seconds)
            if as_utc(now) < next_ok:
                return broadcasts
        mq = await pick_next_question(db, match)
        if mq is None:
            match.status = MATCH_LOCKED
            match.state_version += 1
            await _emit(db, match, "match.auto_locked", {}, broadcasts)
            await audit(db, None, "match.auto_lock", "match", match.id, {})
            return broadcasts
        r = await start_round(db, match, mq, as_utc(now))
        broadcasts.append({"type": protocol.EV_ROUND_OPENED, "match_id": match.id,
                           "payload": {"round_id": r.id, "phase": "display"},
                           "state_version": match.state_version})
        return broadcasts

    if r.status == ROUND_DISPLAYING and as_utc(now) >= as_utc(r.open_at):
        # 观测时钟即权威时钟：控制器是**轮询**推进的，tick 可能比 open_at 晚若干秒。
        # 实测（PostgreSQL 演示赛）客户端拿到的快照里 phase 已是 open、而 open_at 还在未来
        # 2 秒，于是提交命中"作答通道尚未开放"，整场四院 0 分（另一路是列类型的 8 小时偏移，
        # 见 migrations/versions/c9a6e3b70f48）。
        #
        # 收敛方式：把 open_at 直接落到"控制器真正放开的那一刻"，并留一点余量。
        # 两点都要顾到：
        #   1. `received_at` 是**秒级**的（utcnow 截断微秒），若 open_at 带微秒就会比它"晚"，
        #      刚开放的瞬间提交仍会被判 too_early → 所以截断到秒；
        #   2. 控制器 tick 与请求到达之间还有几毫秒到几百毫秒的差 → 再回退一个 tick。
        guard = timedelta(seconds=get_settings().round_tick_s)   # 与 received_at 的秒级粒度对齐
        # 不要早于原定开放时间：控制器迟到一个 tick 时把窗口往前挪一点点是可以的
        # （否则刚开放的瞬间提交会被判 too_early），但**不能**把抢答回合的窗口整体挪到过去——
        # 那会让"作答用时"凭空多算一个 tick，速度分立刻失真。
        # 这个"余量"只用于放行判定的宽容，计时起点另见 submissions/router.py 的 elapsed_ms。
        planned_open = as_utc(r.open_at) or as_utc(now)
        opened = max(planned_open, as_utc(now) - guard).replace(tzinfo=None, microsecond=0)
        r.open_at = opened
        # 截止时间跟着推：控制器迟到的这段时间不该从选手的作答时间里扣。
        # 抢答模式下 opened == 原定开放时刻，因此不会把窗口拉长。
        r.deadline_at = (as_utc(opened) + timedelta(
            seconds=match.answer_seconds)).replace(tzinfo=None)
        r.status = ROUND_OPEN
        broadcasts.append({"type": protocol.EV_ROUND_OPENED, "match_id": match.id,
                           "payload": {"round_id": r.id, "phase": "open"},
                           "state_version": match.state_version})
    elif r.status == ROUND_OPEN and as_utc(now) >= as_utc(r.deadline_at):
        await close_round(db, match, r, as_utc(now))
        broadcasts.append({"type": protocol.EV_ROUND_CLOSED, "match_id": match.id,
                           "payload": {"round_id": r.id},
                           "state_version": match.state_version})
    return broadcasts


# ── 命令执行（幂等 + 状态版本控制，§10.2、§15.1）────────────────────────────


async def execute_command(db: AsyncSession, actor: User, match: Match, command_id: str,
                          action: str, expected_state_version: int,
                          payload: dict) -> dict:
    # 先把要用到的标量取出来存成**局部变量**。
    # 为什么：下面的 IntegrityError 分支会 `await db.rollback()`，而 rollback 会把本会话里
    # 所有 ORM 实例标记为过期；此后连 `match.id` 这种最简单的属性读取都会触发隐式加载，
    # 在 async 会话里直接抛 MissingGreenlet → 500。
    # 实测（并发连点 5 次）：先踩到的是 `match.state_version`，改掉之后再踩到 `match.id` ——
    # 所以这里一次性把需要的值全部落地，函数体内不再依赖 match 的任何属性。
    match_id = match.id
    existing = (await db.execute(select(AdminCommand)
        .where(AdminCommand.command_id == command_id))).scalar_one_or_none()
    if existing is not None:
        return {"status": "replayed", "result": existing.result,
                "error_code": existing.error_code, "action": existing.action,
                "state_version": match.state_version}

    cmd_row = AdminCommand(
        id=new_id(), command_id=command_id, match_id=match_id, action=action,
        expected_state_version=expected_state_version, payload=payload or {},
        result="pending", actor_user_id=actor.id)
    db.add(cmd_row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        again = (await db.execute(select(AdminCommand).where(
            AdminCommand.command_id == command_id))).scalar_one_or_none()
        # 回滚之后一律只查库、不碰 match（见函数开头的说明）
        ver = (await db.execute(
            select(Match.state_version).where(Match.id == match_id))).scalar_one()
        return {"status": "replayed", "result": again.result if again else "unknown",
                "error_code": again.error_code if again else None, "action": action,
                "state_version": int(ver)}

    if expected_state_version != match.state_version:
        cmd_row.result, cmd_row.error_code = "rejected", ErrorCode.STATE_VERSION_CONFLICT
        await db.commit()
        raise AppError(409, ErrorCode.STATE_VERSION_CONFLICT,
                       f"状态版本过期：当前 {match.state_version}，请求基于 {expected_state_version}。请刷新后重试。")

    broadcasts: list[dict] = []
    try:
        async with db.begin_nested():
            if action in sm.SPECIAL_ACTIONS:
                await _run_special(db, actor, match, action, payload or {}, broadcasts)
            else:
                target = sm.ACTION_TARGET.get(action)
                if target is None:
                    raise AppError(422, ErrorCode.VALIDATION_ERROR, f"未知命令：{action}")
                if not sm.can_transition(match.status, target):
                    raise AppError(409, ErrorCode.INVALID_TRANSITION,
                                   f"非法状态转换：{match.status} → {target}")
                handler = _HANDLERS.get(action)
                if handler is not None:
                    await handler(db, actor, match, payload or {}, broadcasts)
                match.status = target
                match.state_version += 1
                await _emit(db, match, "match.state",
                            {"status": target, "action": action}, broadcasts)
                await audit(db, actor.id, f"match.{action}", "match", match.id,
                            payload or {})
            cmd_row.result = "ok"
    except AppError as e:
        cmd_row.result, cmd_row.error_code = "rejected", e.code
        await db.commit()
        raise
    await db.commit()
    for b in broadcasts:
        hub.broadcast(b["match_id"], protocol.message(
            b["type"], b["match_id"], b["payload"], state_version=b["state_version"]))
    return {"status": "ok", "action": action, "state_version": match.state_version,
            "result": "ok"}


async def _run_special(db: AsyncSession, actor: User, match: Match, action: str,
                       payload: dict, broadcasts: list[dict]) -> None:
    if action == "recalculate":
        # 允许的阶段：已算出成绩之后（含揭晓排期与已揭晓）。
        #
        # 曾经只放开"已揭晓后"。但现场真正的时序是：主持人算完成绩 → 大屏还在揭晓动画里
        # （reveal_scheduled）→ 就有学生举手说"XX 用手机查答案"。这个窗口必须能剔除，
        # 否则只能眼睁睁把作弊分数发出去。四院同规则、逐人留理由、生成新版本，
        # 公开口径与审计都在 calculate_result 里，提前放开不降低可追溯性。
        # 未算出成绩前（draft/lobby/running/locked）仍然拒绝：那时还没有成绩版本可更正。
        if match.status not in (MATCH_CALCULATED, MATCH_REVEAL_SCHEDULED,
                                MATCH_REVEALED, MATCH_ARCHIVED):
            raise AppError(409, ErrorCode.INVALID_TRANSITION,
                           "需先 calculate 出成绩，之后才能做赛后更正（剔除作弊卷）。")
        exclusions_raw: dict[str, str] = payload.get("exclusions") or {}
        resolved: dict[str, str] = {}
        for username, reason in exclusions_raw.items():
            u = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
            if u is None:
                raise AppError(404, ErrorCode.NOT_FOUND, f"用户不存在：{username}")
            entry = (await db.execute(select(MatchEntry).where(
                MatchEntry.match_id == match.id, MatchEntry.user_id == u.id))).scalar_one_or_none()
            if entry is None:
                raise AppError(404, ErrorCode.NOT_FOUND, f"该用户不在本场名单：{username}")
            resolved[entry.id] = reason
        if not resolved:
            raise AppError(422, ErrorCode.VALIDATION_ERROR, "recalculate 需要至少一条带理由的剔除。")
        rv = await calculate_result(db, match, actor.id, exclusions=resolved,
                                    is_correction=True, note=payload.get("note"))
        match.state_version += 1
        await _emit(db, match, "result.recalculated",
                    {"result_version_id": rv.id, "version_no": rv.version_no}, broadcasts)
    elif action == "publish_correction":
        if match.status not in (MATCH_REVEALED, "archived"):
            raise AppError(409, ErrorCode.INVALID_TRANSITION, "只能在已揭晓后发布更正版本。")
        rv = (await db.execute(select(ResultVersion)
              .where(ResultVersion.match_id == match.id)
              .order_by(ResultVersion.version_no.desc()).limit(1))).scalar_one_or_none()
        if rv is None or rv.published_at is not None:
            raise AppError(409, ErrorCode.INVALID_TRANSITION, "没有待发布的更正版本。")
        rv.published_at = utcnow()
        match.published_result_version_id = rv.id
        match.state_version += 1
        await _emit(db, match, "result.published",
                    {"result_version_id": rv.id, "version_no": rv.version_no,
                     "correction": True}, broadcasts)
        hub.reset_stream(match.id)


async def _h_open_lobby(db, actor, match, payload, broadcasts) -> None:
    """开启大厅（后台"开启竞赛"）。可同时设定预定开赛时间，供首页倒计时使用。

    注意：`start_at` 只影响**展示**（首页倒计时）。真正开赛仍由主持人执行
    `start_match`，因此到点没点开赛也不会出现"倒计时结束却答不了题"的错误状态——
    首页会保持在"即将开始"而不是假装已开赛。
    """
    mains = (await db.execute(select(func.count()).select_from(MatchQuestion)
                              .where(MatchQuestion.match_id == match.id,
                                     MatchQuestion.is_backup.is_(False)))).scalar_one()
    if mains != match.planned_questions:
        raise AppError(409, ErrorCode.VALIDATION_ERROR,
                       f"题单未配置完整：主题 {mains}/{match.planned_questions}")
    raw = payload.get("start_at")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            raise AppError(422, ErrorCode.VALIDATION_ERROR, f"start_at 时间格式错误：{raw}")
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        match.start_at = dt
    elif payload.get("clear_start_at"):
        match.start_at = None


async def _h_start_match(db, actor, match, payload, broadcasts) -> None:
    info = await _freeze_roster(db, match, bool(payload.get("allow_incomplete", False)))
    await _emit(db, match, "match.roster_frozen", info, broadcasts)


async def _h_pause(db, actor, match, payload, broadcasts) -> None:
    reason = payload.get("reason") or "主持人暂停"
    # 接力赛同时开着多个回合，必须全部按同一规则处理（四院一致，§10.3）
    voided = await close_all_open_rounds(db, match, utcnow(), f"pause: {reason}")
    for rid in voided:
        await _emit(db, match, "round.voided", {"round_id": rid, "reason": reason}, broadcasts)


async def _h_lock(db, actor, match, payload, broadcasts) -> None:
    closed = await close_all_open_rounds(db, match, utcnow(), "lock")
    for rid in closed:
        await _emit(db, match, protocol.EV_ROUND_CLOSED, {"round_id": rid}, broadcasts)


async def _h_calculate(db, actor, match, payload, broadcasts) -> None:
    rv = await calculate_result(db, match, actor.id)
    await _emit(db, match, "result.calculated",
                {"result_version_id": rv.id, "version_no": rv.version_no,
                 "is_official": rv.is_official}, broadcasts)


async def _h_schedule_reveal(db, actor, match, payload, broadcasts) -> None:
    def _parse(x):
        if not x:
            raise AppError(422, ErrorCode.VALIDATION_ERROR, "需要 ceremony_start_at 与 reveal_at。")
        try:
            return datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        except ValueError:
            raise AppError(422, ErrorCode.VALIDATION_ERROR, f"时间格式错误：{x}")
    ceremony = _parse(payload.get("ceremony_start_at"))
    reveal = _parse(payload.get("reveal_at"))
    now = as_utc(utcnow())
    if ceremony.tzinfo is not None:
        ceremony = ceremony.astimezone(timezone.utc).replace(tzinfo=None)
    if reveal.tzinfo is not None:
        reveal = reveal.astimezone(timezone.utc).replace(tzinfo=None)
    if not (as_utc(ceremony) > now):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "ceremony_start_at 必须晚于当前服务器时间。")
    if not (as_utc(reveal) > as_utc(ceremony)):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "reveal_at 必须晚于 ceremony_start_at。")
    match.ceremony_start_at = ceremony
    match.reveal_at = reveal
    await _emit(db, match, protocol.EV_RESULT_SCHEDULED,
                {"ceremony_start_at": as_utc(ceremony).isoformat(),
                 "reveal_at": as_utc(reveal).isoformat()}, broadcasts)


async def _h_cancel(db, actor, match, payload, broadcasts) -> None:
    if match.current_round_id:
        r = await db.get(RoundInstance, match.current_round_id)
        if r is not None and r.status in (ROUND_DISPLAYING, ROUND_OPEN):
            await void_round(db, match, r, f"cancel: {payload.get('reason') or ''}")


_HANDLERS = {
    "open_lobby": _h_open_lobby,
    "start_match": _h_start_match,
    "pause": _h_pause,
    "resume": None,
    "lock": _h_lock,
    "calculate": _h_calculate,
    "schedule_reveal": _h_schedule_reveal,
    "cancel": _h_cancel,
}


async def recover_on_startup(db: AsyncSession) -> int:
    """服务重启恢复（§10.3）：进行中的比赛 → 待主持人确认的暂停；已曝光回合作废+援助退回。"""
    live = (await db.execute(select(Match).where(
        Match.status.in_([MATCH_RUNNING, MATCH_PAUSED])))).scalars().all()
    n = 0
    for match in live:
        broadcasts: list[dict] = []
        if match.current_round_id:
            r = await db.get(RoundInstance, match.current_round_id)
            if r is not None and r.status in (ROUND_DISPLAYING, ROUND_OPEN):
                await void_round(db, match, r, "server_restart")
                await _emit(db, match, "round.voided",
                            {"round_id": r.id, "reason": "server_restart"}, broadcasts)
        if match.status == MATCH_RUNNING:
            match.status = MATCH_PAUSED
            match.state_version += 1
            await _emit(db, match, "match.state", {"status": MATCH_PAUSED,
                                                   "action": "server_restart"}, broadcasts)
        await audit(db, None, "match.recover", "match", match.id, {})
        await db.commit()
        for b in broadcasts:
            hub.broadcast(b["match_id"], protocol.message(
                b["type"], b["match_id"], b["payload"], state_version=b["state_version"]))
        n += 1
    return n
