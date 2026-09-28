"""比赛 HTTP 路由：签到、快照、管理端建赛/名单/命令（§14.1）。"""
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    get_current_user,
    get_db,
    get_optional_user,
    require_admin,
    require_house_member,
)
from app.core.errors import AppError, ErrorCode
from app.core.security import new_id
from app.db.base import as_utc, iso_z, server_time_iso
from app.db.models import (
    AdminCommand,
    Answer,
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
    MATCH_DRAFT,
    MATCH_LOBBY,
    ROLE_ADMIN,
)
from app.modules.audit.service import audit
from app.modules.competitions import rules
from app.modules.competitions import service
from app.modules.competitions import snapshot as snapshot_mod
from app.modules.competitions.snapshot import build_snapshot

router = APIRouter(prefix="/api/v1", tags=["competitions"])


@router.post("/matches/{match_id}/check-in")
async def check_in(match_id: str, user: User = Depends(require_house_member),
                   db: AsyncSession = Depends(get_db)) -> dict:
    """签到参赛。必须登录且已绑定 House（见 require_house_member）。"""
    return await service.check_in(db, user, match_id)


@router.get("/matches/{match_id}/board")
async def public_board(match_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """匿名可读的大屏视图（投屏/走廊希沃用，无需登录）。

    **不含题目内容**：只有比赛状态、回合阶段与时间、四院聚合进度、揭晓后的结果。
    观众不需要账号就能看懂场上在发生什么；但下一场的选手提前看到题目就等于泄题，
    所以这条链路里根本没有题干与选项字段。
    """
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    return await snapshot_mod.build_public_board(db, match)


@router.get("/matches/{match_id}/replay")
async def replay(match_id: str, public: int = 0,
                 viewer: User | None = Depends(get_optional_user),
                 db: AsyncSession = Depends(get_db)) -> dict:
    """镜像回放：把整场按**作答时间线**重放出来。

    两种访问方式，对应两种真实场景：

    · **已登录**（默认）：完整回放。含匿名别名、每题选项与用时；`correct` 只在
      结果已发布后给出——否则回放就成了"提前对答案"的通道。
    · **`?public=1`（匿名）**：给大屏/走廊希沃用的**公开回放**。只给"每院在第几题、
      用了几秒"，**不含别名、不含选项、绝不含对错**。走廊屏幕上不该出现任何
      个人标识，也不该在下一场开赛前泄露任何题目信息。

    数据从哪来：`answers.received_at` 与 `elapsed_ms` 本来就是服务端受理时记录的，
    因此时间线不需要额外存储。
    """
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")

    is_admin = viewer is not None and viewer.role == ROLE_ADMIN
    public_view = bool(public) or viewer is None
    revealed = match.status in ("revealed", "archived")
    my_house = await db.get(House, viewer.house_id) if (viewer and viewer.house_id) else None
    my_key = my_house.key if my_house else None
    scope = match.houses_scope or None
    if not public_view and not is_admin and scope and not revealed and my_key not in scope:
        raise AppError(403, ErrorCode.NOT_YOUR_HOUSE,
                       "本场比赛与你所在的学院无关，结果公布后即可查看回放。",
                       {"scope": scope, "your_house": my_key})

    rounds = (await db.execute(
        select(RoundInstance).where(RoundInstance.match_id == match.id)
        .order_by(RoundInstance.created_at))).scalars().all()
    questions = {mq.id: mq for mq in (await db.execute(
        select(MatchQuestion).where(MatchQuestion.match_id == match.id))).scalars().all()}
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}
    entries = {e.id: e for e in (await db.execute(
        select(MatchEntry).where(MatchEntry.match_id == match.id))).scalars().all()}

    answers = (await db.execute(
        select(Answer).where(Answer.match_id == match.id)
        .order_by(Answer.received_at))).scalars().all()

    correct_map: dict[str, str] = {}
    if revealed:
        # 只有揭晓后才需要（也要）知道正确答案
        from app.modules.scoring.service import _round_correct_map
        correct_map = await _round_correct_map(db, [r.id for r in rounds])

    t0 = min((as_utc(r.open_at) for r in rounds if r.open_at), default=None)
    round_out = []
    for r in rounds:
        mq = questions.get(r.match_question_id)
        scope_h = getattr(r, "house_scope", None)
        item = {
            "round_instance_id": r.id,
            "status": r.status,
            "house_scope": scope_h,
            "position": mq.position if (mq is not None and not mq.is_backup) else None,
            "is_backup": bool(mq.is_backup) if mq is not None else False,
            "open_at": iso_z(r.open_at),
            "deadline_at": iso_z(r.deadline_at),
            "offset_ms": (int((as_utc(r.open_at) - t0).total_seconds() * 1000)
                          if (t0 and r.open_at) else None),
        }
        round_out.append(item)

    events = []
    for a in answers:
        e = entries.get(a.entry_id)
        if e is None:
            continue
        hk = houses[e.house_id].key if e.house_id in houses else None
        ev = {
            "round_instance_id": a.round_instance_id,
            "house": hk,
            "elapsed_ms": a.elapsed_ms,
            "received_at": iso_z(a.received_at),
        }
        if not public_view:
            # 个人标识与选项只在登录视图里出现；公开回放（走廊大屏）不带任何个人痕迹
            ev["alias"] = e.display_alias
            ev["option_id"] = a.option_id
            ev["correct"] = (correct_map.get(a.round_instance_id) == a.option_id) if revealed else None
        events.append(ev)

    return {
        "match": {"id": match.id, "title": match.title, "status": match.status,
                  "competition_mode": getattr(match, "competition_mode", None) or "shared_buzz",
                  "questions_per_house": getattr(match, "questions_per_house", None),
                  "revealed": revealed},
        "server_time": server_time_iso(),
        "viewer": {"house_key": my_key, "is_admin": is_admin, "anonymous": public_view},
        "started_at": iso_z(t0),
        "rounds": round_out,
        "events": events,
        "note": ("按服务端记录的作答时间线重放；回放中的用时与得分口径一致。"
                 + ("公开回放不含个人标识与选项。" if public_view else
                    "" if revealed else "成绩公布前不显示每题对错。")),
    }


@router.get("/matches/{match_id}/snapshot")
async def snapshot(match_id: str, request_user: User | None = Depends(get_optional_user),
                   db: AsyncSession = Depends(get_db)) -> dict:
    """答题快照。

    权限规则（逐条强制，不依赖前端隐藏）：
      · **未登录**：拒绝（LOGIN_REQUIRED）。竞赛相关一律需要身份。
      · **无 House 的学生**：拒绝（HOUSE_REQUIRED）——否则会产生不计分的幽灵选手。
      · **非本场名单的登录用户**：只给公开聚合进度（观赛视图），不给任何题目内容。
      · **本场名单成员**：完整答题视图。
    """
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    if request_user is None:
        raise AppError(401, ErrorCode.LOGIN_REQUIRED,
                       "比赛页面需要登录后访问。请先登录。")
    if request_user.role != ROLE_ADMIN and request_user.house_id is None:
        raise AppError(403, ErrorCode.HOUSE_REQUIRED,
                       "你的账号还没有归属学院，无法参赛或观赛。请联系管理员分配学院。",
                       {"needs_house": True})
    return await build_snapshot(db, request_user, match)


@router.get("/matches/{match_id}/spectate")
async def spectate(match_id: str, viewer: User | None = Depends(get_optional_user),
                   db: AsyncSession = Depends(get_db)) -> dict:
    """观战数据（只读大屏用）。**匿名可读**；登录后按身份给更多。

    为什么要这些门槛：
      · 未登录没有身份，无从判断该看哪个学院，且比赛期间会被匿名流量压垮；
      · 无 House 的账号不属于任何一方，看别院进度没有意义。
      ↑ 以上是 2026-09-26 之前的理由。产品负责人本轮要求
        「首页的观赛大屏无论是否登录都可以查看」「管理员可以任意查看 House 比赛」，
        于是门槛改为**分层**而不是**拦截**：
          匿名 / 未绑定学院 → 公开总览（四院聚合进度 + 第几题 + 提交矩阵 + 揭晓后结果）
          本院学生          → 公开总览 + 本院进度与自己这一格的标记
          管理员            → 公开总览 + **四院全部**标记为"本院"，可任选一院下钻
        唯一仍然拦截的情形是 `houses_scope`（限定范围赛）未揭晓时：
        匿名无法核验"是否属于被排除的学院"，放行等于让被排除学院的学生退出登录即可绕过。

    与 `/snapshot` 的分工：snapshot 是"我自己要答题"的完整视图（含我的回执/援助）；
    观战只需要**看得懂场上在发生什么**，因此这里做四件事：
      1. **绝不返回题目内容与标准答案** —— 观战者可能是下一场的选手，提前看到题目等于泄题。
         只给"第几题/共几题"这种进度信息。
      2. **绝不返回未揭晓的比分**（§10.4 揭晓保护对观战同样生效）。
      3. **未揭晓前只给"自己学院"的可见范围**：四院聚合进度照给（这是全场公开信息，
         大屏上本来就显示），但**不返回题目、不给其他学院的可下钻细节**；
         揭晓后才放开完整结果。管理员不受此限（主持本就需要全局视野）。
      4. 个人维度按 `match.live_visibility` 裁剪，且 individual 档**只列匿名别名**
         （`tiger-03`），不含任何真实身份。
    """
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")

    is_anon = viewer is None
    is_admin = viewer is not None and viewer.role == ROLE_ADMIN
    revealed = match.status in ("revealed", "archived")
    # 本院 key：观战时用来裁剪"本院专属"信息；匿名/管理员都没有"本院"
    my_house = await db.get(House, viewer.house_id) if (viewer and viewer.house_id) else None
    my_key = my_house.key if my_house else None

    # 红线：**未揭晓前，不属于本场的学院看不到本场**。
    # 限定范围赛（houses_scope）里，被排除的学院在结果公布前连进度都不该看到——
    # 否则等于提前窥探对手节奏。揭晓后放开（结果本来就是公开的）。
    # 匿名同样拦：否则"退出登录"就是绕过这条规则的现成办法。
    scope = match.houses_scope or None
    if not revealed and scope:
        if is_anon:
            raise AppError(403, ErrorCode.NOT_YOUR_HOUSE,
                           "本场为限定范围赛，登录后方可查看进度。",
                           {"scope": scope, "anonymous": True})
        if not is_admin and my_key not in scope:
            hs = {h.key: h.name_cn for h in (await db.execute(select(House))).scalars()}
            raise AppError(403, ErrorCode.NOT_YOUR_HOUSE,
                           "本场比赛与你所在的学院无关，结果公布后即可查看。",
                           {"scope": scope, "your_house": my_key})

    progress = await snapshot_mod._progress(db, match)
    vis = getattr(match, "live_visibility", None) or "house"

    # 未揭晓时，只把"本院"这一格标出来；其余学院仍给聚合数字（大屏本来就公开展示），
    # 但不提供任何按院下钻的细节，避免观战者拿到别院的战术信息。
    # 管理员例外：他要能"任意查看 House"，因此四院全部标记为可下钻。
    houses_out = []
    for h in progress.get("houses", []):
        row = dict(h)
        row["is_mine"] = is_admin or (h.get("key") == my_key)
        houses_out.append(row)
    progress = {**progress, "houses": houses_out}

    out: dict = {
        "match": {"id": match.id, "title": match.title, "status": match.status,
                  "mode": match.mode, "state_version": match.state_version,
                  "planned_questions": match.planned_questions,
                  "answer_seconds": match.answer_seconds,
                  "live_visibility": vis},
        "server_time": server_time_iso(),
        "progress": progress,
        "viewer": {"role": viewer.role if viewer else "anonymous", "house_key": my_key,
                   "is_admin": is_admin, "revealed": revealed, "anonymous": is_anon},
    }

    # 当前环节（只有阶段名与题目序号，没有题干）
    phase = None
    if match.current_round_id:
        r = await db.get(RoundInstance, match.current_round_id)
        if r is not None and r.status in ("displaying", "open"):
            mq = await db.get(MatchQuestion, r.match_question_id)
            total = (await db.execute(
                select(func.count()).select_from(MatchQuestion)
                .where(MatchQuestion.match_id == match.id, MatchQuestion.is_backup.is_(False))
            )).scalar_one()
            phase = {
                "round_instance_id": r.id,
                "position": mq.position if (mq and not mq.is_backup) else None,
                "total": total,
                "phase": "reading" if r.status == "displaying" else "answering",
                "deadline_at": snapshot_mod._iso(r.deadline_at),
                "open_at": snapshot_mod._iso(r.open_at),
            }
    out["phase"] = phase

    # individual 档：**只列匿名别名**（`tiger-03`，不含任何真实身份）。
    # 本院观战者只看本院；管理员四院全给 —— 他要能"任意查看 House 比赛"。
    if vis == "individual" and match.current_round_id and (my_key or is_admin):
        stmt = (
            select(MatchEntry.display_alias, MatchEntry.house_key_snapshot, Answer.entry_id)
            .select_from(MatchEntry)
            .outerjoin(Answer, (Answer.entry_id == MatchEntry.id)
                       & (Answer.round_instance_id == match.current_round_id))
            .where(MatchEntry.match_id == match.id, MatchEntry.checked_in_at.is_not(None))
            .order_by(MatchEntry.display_alias)
        )
        if not is_admin:
            stmt = stmt.where(MatchEntry.house_key_snapshot == my_key)
        rows = (await db.execute(stmt)).all()
        out["individuals"] = [
            {"alias": alias, "house_key": hk, "submitted": entry_id is not None}
            for alias, hk, entry_id in rows
        ]

    # 已揭晓才给结果（与 snapshot 同一道门）
    if match.status in ("revealed", "archived") and match.published_result_version_id:
        rv = await db.get(ResultVersion, match.published_result_version_id)
        if rv is not None and rv.published_at is not None:
            from app.modules.scoring.service import results_payload
            out["result"] = await results_payload(db, match, rv)
    return out


@router.get("/matches/{match_id}/spectate/live")
async def spectate_live(match_id: str, viewer: User | None = Depends(get_optional_user),
                        db: AsyncSession = Depends(get_db)) -> dict:
    """观战实时画面（双屏用）。按 `match.spectator_mode` 决定给到哪一档。

    三档的实际差别（这是产品决策，不是实现细节）：

      mirror  与参赛者画面**逐像素一致**：题干、选项、倒计时、作答阶段全给。
              为什么可以开：观战已被限定为"只能看自己 House 的比赛"，
              而本院选手本来就能在答题页看到同样的内容 —— 镜像不产生任何额外泄露。
              用途：观众席大屏、班级投影，让场下的人跟场上同步紧张。

      blur    版式与节奏一致，但**题干与选项文本不下发**（前端对占位块做模糊），
              只有倒计时、回合号、进度是真的。用途：观众里有非参赛选手时更稳妥。

      summary 只给进度矩阵，不出现任何题目区域。用途：最保守的公开场合。

    重要：**任何档位都不返回标准答案**。镜像是"看到题目"，不是"看到答案"。

    ── 2026-09-26 变更（两轮）：
       第一轮：本接口允许未登录访问（产品负责人："首页的观赛大屏无论是否登录都可以查看"）。
       第二轮（产品负责人进一步明确）：**能不能看到题面，取决于赛制，不取决于是否登录** ——

         · 抢答赛（shared_buzz）：四院拿到**同一道题、同一个倒计时**，
           题目在同一时刻对所有选手都是公开的 ⇒ 未登录观看**不产生额外泄露**，
           照常按本场 spectator_mode 下发题面。
         · 接力赛（relay）：**每院各自成组题**（`questions_per_house`），
           A 院的题面公开出去就等于泄露给 B/C/D 院 ⇒ **对任何人都不下发题面**，
           一律降到 summary 档（只给进度矩阵与节奏）。

       所以原先"匿名一律强制 summary"的做法过严（抢答赛本可以给），
       而"接力赛照旧 mirror"则是**真的会漏题**（连已登录的别院学生也会看到）。
       两处一并按赛制修正。
    """
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")

    is_anon = viewer is None
    host_mode = getattr(match, "spectator_mode", None) or "mirror"
    competition = getattr(match, "competition_mode", None) or "shared_buzz"
    # 接力赛封顶在 summary：每院独立成组题，题面不下发给任何人。
    # 抢答赛尊重主持人设的档位（mirror 给题面 / blur 模糊 / summary 只给进度）。
    mode = "summary" if competition == "relay" else host_mode
    is_admin = viewer is not None and viewer.role == ROLE_ADMIN
    revealed = match.status in ("revealed", "archived")
    my_house = await db.get(House, viewer.house_id) if (viewer and viewer.house_id) else None
    my_key = my_house.key if my_house else None

    scope = match.houses_scope or None
    if not revealed and scope:
        if is_anon:
            raise AppError(403, ErrorCode.NOT_YOUR_HOUSE,
                           "本场为限定范围赛，登录后方可查看进度。",
                           {"scope": scope, "anonymous": True})
        if not is_admin and my_key not in scope:
            raise AppError(403, ErrorCode.NOT_YOUR_HOUSE,
                           "本场比赛与你所在的学院无关，结果公布后即可查看。",
                           {"scope": scope, "your_house": my_key})

    out: dict = {
        "match": {"id": match.id, "title": match.title, "status": match.status,
                  "mode": match.mode, "state_version": match.state_version,
                  "planned_questions": match.planned_questions,
                  "answer_seconds": match.answer_seconds,
                  "display_seconds": match.display_seconds,
                  "live_visibility": getattr(match, "live_visibility", "house"),
                  "spectator_mode": mode,
                  "competition_mode": getattr(match, "competition_mode", None) or "shared_buzz",
                  "questions_per_house": getattr(match, "questions_per_house", None),
                  "buzz": int(match.display_seconds or 0) <= 0},
        "server_time": server_time_iso(),
        "viewer": {"role": viewer.role if viewer else "anonymous", "house_key": my_key,
                   "is_admin": is_admin, "revealed": revealed, "anonymous": is_anon},
        "matrix": await snapshot_mod.submission_matrix(db, match),        # 观战大屏的信息密度：光有"第几题 + 四院进度"太空，
        # 逐题节奏与最近提交流水都是聚合量，不含题面/答案/身份，可以放心给。
        "timeline": await snapshot_mod.round_timeline(db, match),
        "recent": await snapshot_mod.recent_submissions(db, match),
    }

    # 与比赛页同构的"当前环节"信息
    room: dict | None = None
    if match.current_round_id:
        r = await db.get(RoundInstance, match.current_round_id)
        if r is not None and r.status in ("displaying", "open"):
            mq = await db.get(MatchQuestion, r.match_question_id)
            total = (await db.execute(
                select(func.count()).select_from(MatchQuestion)
                .where(MatchQuestion.match_id == match.id, MatchQuestion.is_backup.is_(False))
            )).scalar_one()
            room = {
                "round_instance_id": r.id,
                "position": mq.position if (mq and not mq.is_backup) else None,
                "total": total,
                "phase": "reading" if r.status == "displaying" else "answering",
                "display_at": snapshot_mod._iso(r.display_at),
                "open_at": snapshot_mod._iso(r.open_at),
                "deadline_at": snapshot_mod._iso(r.deadline_at),
            }
            # 只有 mirror 档下发题面；blur/summary 一律不给文本（前端据 mode 决定怎么画）
            if mode == "mirror" and mq is not None:
                v = await db.get(QuestionVersion, mq.version_id)
                if v is not None:
                    room["prompt"] = v.prompt
                    room["options"] = [{"option_id": o["option_id"], "text": o["text"]}
                                       for o in v.options_json]
    out["room"] = room

    # 四院进度：**匿名也给**。它是全场公开信息（投屏大屏 /board 一直就给），
    # 而且产品负责人要的"全 House 答题情况总览"正是它。
    # 本院进度（my_house_progress）仍然只有绑定了学院的账号才有 —— 匿名没有"本院"。
    prog = await snapshot_mod._progress(db, match)
    out["progress"] = prog
    if my_key:
        mine = next((h for h in prog.get("houses", []) if h.get("key") == my_key), None)
        out["my_house_progress"] = mine

    if match.status in ("revealed", "archived") and match.published_result_version_id:
        rv = await db.get(ResultVersion, match.published_result_version_id)
        if rv is not None and rv.published_at is not None:
            from app.modules.scoring.service import results_payload
            out["result"] = await results_payload(db, match, rv)
    return out


# ── 管理端 ───────────────────────────────────────────────────────────────────


class MatchCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    mode: str = "fixed_k"
    fixed_k: int | None = Field(default=None, ge=1, le=50)
    planned_questions: int = Field(default=20, ge=1, le=100)
    # 题源：random 随机 / custom 自定义 / blueprint 主题配额（不传则按参数推断）
    question_source: str | None = Field(default=None, pattern="^(random|custom|blueprint)$")
    question_ids: list[str] | None = None
    blueprint: dict[str, int] | None = None
    # 难度配比（随机题源可用）：{"1": 6, "2": 10, "3": 4}
    difficulty_mix: dict[str, int] | None = None
    # 题库（大类）：official = 竞赛题库（默认），practice = 日常题库。
    # 选完之后再在 blueprint 里按**小类（主题）**分配题量，两级一起决定这一场抽哪些题。
    # 用日常题库办赛是允许的：学生可能练过那些题，因此界面会明确提示这一点。
    question_pool: str | None = Field(default=None, pattern="^(official|practice)$")
    # 评分标准：avg 人均平均 / total 总分 / top_n 前 N 名平均 / accuracy 正确率
    scoring_method: str = Field(default="avg", pattern="^(avg|total|top_n|accuracy)$")
    scoring_top_n: int | None = Field(default=None, ge=1, le=50)
    # 赛制（双模式）：
    #   shared_buzz 一次竞赛一个大题库，四院同题抢答
    #   relay       每个 House 固定题目数量，比总用时与每题用时（支持镜像回放）
    competition_mode: str = Field(default="shared_buzz", pattern="^(shared_buzz|relay)$")
    # 接力赛每院题目数量（接力必填）。全场题量 = 每院题量 × 参赛学院数。
    questions_per_house: int | None = Field(default=None, ge=1, le=60)
    # 接力赛时间权重（0–1000），与正确率权重合成
    weight_time: int | None = Field(default=None, ge=0, le=1000)
    # 评分模式与权重（见 models 常量区说明）。
    #   pure      只看正确率
    #   composite 综合评估：正确率 + 抢答速度（时间），权重按需配
    scoring_mode: str = Field(default="pure", pattern="^(pure|composite)$")
    weight_accuracy: int | None = Field(default=None, ge=0, le=1000)
    weight_speed: int | None = Field(default=None, ge=0, le=1000)
    # 参与度权重：0 = 不计入（默认）。启用后按"本院实际上场人数 ÷ 冻结名单人数"加权。
    weight_participation: int | None = Field(default=None, ge=0, le=1000)
    min_house_size: int | None = Field(default=None, ge=1)
    # 读题秒：**0 = 抢答模式**（题目一出现即可作答，本系统默认赛制）。
    # >0 时保留"先看后答"的读题窗口，用于需要主持人念题的场次。
    display_seconds: int | None = Field(default=None, ge=0, le=120)
    answer_seconds: int | None = Field(default=None, ge=1, le=300)
    interlude_seconds: int | None = Field(default=None, ge=0, le=60)
    aid_enabled: bool = True
    aid_window_seconds: int | None = Field(default=None, ge=3, le=30)
    aid_quota_per_house: int | None = Field(default=None, ge=0, le=3)
    # 观战档位与参赛范围：此前只有 API 能设、界面没有入口，等于"产品要求里有、现场用不到"。
    # 建赛时可选，之后也能用 PATCH /admin/matches/{id} 改。
    spectator_mode: str | None = Field(default=None, pattern="^(mirror|blur|summary)$")
    houses_scope: list[str] | None = None


class MatchPatchIn(BaseModel):
    """比赛的可调项。

    两类字段的边界不一样：
      · **计分口径**（scoring_mode / 权重 / 读题秒 / 题量 / 时长）：只在 draft/lobby 可改。
        开赛后改等于把已经打过的回合按新规则重算，学院之间立刻不可比——这是系统不变量。
      · **观战档位 / 标题 / 援助开关**：随时可改，它们不影响比赛本身，只影响观众看到什么。
    """
    spectator_mode: str | None = Field(default=None, pattern="^(mirror|blur|summary)$")
    houses_scope: list[str] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=128)
    aid_enabled: bool | None = None
    # 计分口径（赛前锁定）
    scoring_method: str | None = Field(default=None, pattern="^(avg|total|top_n|accuracy)$")
    scoring_mode: str | None = Field(default=None, pattern="^(pure|composite)$")
    weight_accuracy: int | None = Field(default=None, ge=0, le=1000)
    weight_speed: int | None = Field(default=None, ge=0, le=1000)
    weight_participation: int | None = Field(default=None, ge=0, le=1000)
    weight_time: int | None = Field(default=None, ge=0, le=1000)
    questions_per_house: int | None = Field(default=None, ge=1, le=60)
    display_seconds: int | None = Field(default=None, ge=0, le=120)


@router.patch("/admin/matches/{match_id}")
async def patch_match(match_id: str, body: MatchPatchIn,
                      admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """赛前可调项 + 赛中可调项。

    边界（见 MatchPatchIn 说明）：计分口径与时长只允许在 draft/lobby 阶段修改；
    观战档位、标题、援助开关随时可改。
    """
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    changed: dict[str, Any] = {}
    if body.spectator_mode is not None:
        match.spectator_mode = body.spectator_mode
        changed["spectator_mode"] = body.spectator_mode
    if body.aid_enabled is not None:
        match.aid_enabled = body.aid_enabled
        changed["aid_enabled"] = body.aid_enabled
    if body.title is not None:
        match.title = body.title
        changed["title"] = body.title
    if body.houses_scope is not None:
        if match.status not in (MATCH_DRAFT, MATCH_LOBBY):
            raise AppError(409, ErrorCode.MATCH_NOT_JOINABLE,
                           "参赛学院范围只能在开大厅阶段之前调整。")
        valid = {h.key for h in (await db.execute(select(House))).scalars()}
        bad = [k for k in body.houses_scope if k not in valid]
        if bad:
            raise AppError(422, ErrorCode.VALIDATION_ERROR, f"未知学院：{'、'.join(bad)}")
        match.houses_scope = body.houses_scope or None
        changed["houses_scope"] = match.houses_scope

    # ── 计分口径：只在开赛前可改（赛前冻结是系统不变量，不是提示） ──
    scoring_fields = (body.scoring_method, body.scoring_mode, body.weight_accuracy,
                      body.weight_speed, body.display_seconds, body.weight_participation)
    if any(v is not None for v in scoring_fields):
        if match.status not in (MATCH_DRAFT, MATCH_LOBBY):
            raise AppError(409, ErrorCode.INVALID_TRANSITION,
                           "计分口径与读题时长只能在开赛前调整：开赛后改动会让已打过的回合"
                           "与新规则不一致，学院之间不再可比。")
        if body.scoring_method is not None:
            match.scoring_method = body.scoring_method
            changed["scoring_method"] = body.scoring_method
        if body.scoring_mode is not None:
            match.scoring_mode = body.scoring_mode
            changed["scoring_mode"] = body.scoring_mode
        if body.weight_accuracy is not None:
            match.weight_accuracy = body.weight_accuracy
            changed["weight_accuracy"] = body.weight_accuracy
        if body.weight_speed is not None:
            match.weight_speed = body.weight_speed
            changed["weight_speed"] = body.weight_speed
        if body.weight_time is not None:
            match.weight_time = body.weight_time
            changed["weight_time"] = body.weight_time
        if body.weight_participation is not None:
            match.weight_participation = body.weight_participation
            changed["weight_participation"] = body.weight_participation
        if body.questions_per_house is not None:
            # 接力赛改每院题量会连带改变全场题量（题库是赛前冻结的），
            # 只有还来得及重新抽题的阶段才允许；一旦已冻结题目就拒绝。
            if getattr(match, "competition_mode", None) != "relay":
                raise AppError(422, ErrorCode.VALIDATION_ERROR,
                               "questions_per_house 只对接力赛有效。")
            need = body.questions_per_house * (len(match.houses_scope)
                                               if match.houses_scope else len(ACTIVE_HOUSE_KEYS))
            if need > (match.planned_questions or 0):
                raise AppError(422, ErrorCode.VALIDATION_ERROR,
                               f"每院 {body.questions_per_house} 题需要全场 {need} 道题，"
                               f"但本场只冻结了 {match.planned_questions} 道。请重建比赛。")
            match.questions_per_house = body.questions_per_house
            changed["questions_per_house"] = body.questions_per_house
        if body.display_seconds is not None:
            match.display_seconds = body.display_seconds
            changed["display_seconds"] = body.display_seconds
            changed["buzz"] = body.display_seconds == 0
        # 校验综合评估的权重组合（与建赛同一套规则，避免改完变成全员 0 分）
        if match.scoring_mode == "composite" \
                and (match.weight_accuracy or 0) + (match.weight_speed or 0) \
                + (match.weight_participation or 0) <= 0:
            raise AppError(422, ErrorCode.VALIDATION_ERROR,
                           "综合评估模式下必须至少给正确率、速度或参与度一个非零权重。")

    if not changed:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "没有需要修改的字段。")
    await audit(db, admin.id, "match.patch", "match", match.id, changed)
    await db.commit()
    return {"ok": True, "changed": changed, "status": match.status}


class QuotaPreviewIn(BaseModel):
    planned_questions: int = Field(default=20, ge=1, le=100)
    difficulty_mix: dict[str, int] | None = None
    blueprint: dict[str, int] | None = None
    # 题库（大类）：official = 竞赛题库（默认），practice = 日常题库。
    # 预览必须跟着所选题库走，否则切到日常题库后看到的还是竞赛题库的家底。
    question_pool: str | None = None


@router.post("/admin/matches/quota-preview")
async def quota_preview(body: QuotaPreviewIn, admin: User = Depends(require_admin),
                        db: AsyncSession = Depends(get_db)) -> dict:
    """建赛前的配额预览：先确认「这样抽能不能出题」，再真正建赛。

    现场最怕点了建赛才发现题不够。这里返回可用量、缺口与建议。
    """
    return await service.quota_preview(db, body.planned_questions,
                                       body.difficulty_mix, body.blueprint,
                                       pool=service.normalize_pool(body.question_pool))


@router.post("/admin/matches")
async def create_match(body: MatchCreateIn, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)) -> dict:
    match = await service.create_match(db, admin, body.model_dump())
    pool_rows = (await db.execute(
        select(Question.pool, func.count())
        .join(MatchQuestion, MatchQuestion.question_id == Question.id)
        .where(MatchQuestion.match_id == match.id)
        .group_by(Question.pool)
    )).all()
    pools_present = [{"pool": p, "label": service.pool_label(p), "count": int(n)}
                     for p, n in sorted(pool_rows, key=lambda x: -x[1])]
    return {"match_id": match.id, "title": match.title, "status": match.status,
            "planned_questions": match.planned_questions,
            "min_valid_questions": match.min_valid_questions,
            "question_source": match.question_source,
            "question_pool": pools_present[0]["pool"] if pools_present else None,
            "question_pool_label": pools_present[0]["label"] if pools_present else None,
            "competition_mode": getattr(match, "competition_mode", None) or "shared_buzz",
            "questions_per_house": getattr(match, "questions_per_house", None),
            "scoring_method": match.scoring_method,
            "scoring_mode": getattr(match, "scoring_mode", None) or "pure",
            "scoring_top_n": match.scoring_top_n}


@router.get("/admin/matches")
async def list_matches(page: int = 1, size: int = 50, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(Match).order_by(Match.created_at.desc())
                             .limit(size).offset((page - 1) * size))).scalars().all()
    return {"items": [{"id": m.id, "title": m.title, "mode": m.mode, "status": m.status,
                       "state_version": m.state_version, "planned_questions": m.planned_questions,
                       "fixed_k": m.fixed_k, "created_at": iso_z(m.created_at),
                       "competition_mode": getattr(m, "competition_mode", None) or "shared_buzz",
                       "questions_per_house": getattr(m, "questions_per_house", None),
                       "question_source": m.question_source,
                       "scoring_method": m.scoring_method,
                       "scoring_mode": getattr(m, "scoring_mode", None) or "pure",
                       "scoring_top_n": m.scoring_top_n,
                       "start_at": iso_z(m.start_at)} for m in rows],
            "page": page, "size": size}


async def _match_or_404(db: AsyncSession, match_id: str) -> Match:
    m = await db.get(Match, match_id)
    if m is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    return m


@router.get("/admin/matches/{match_id}")
async def match_detail(match_id: str, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)) -> dict:
    m = await _match_or_404(db, match_id)
    entries = (await db.execute(select(MatchEntry, User).join(User, User.id == MatchEntry.user_id)
                                .where(MatchEntry.match_id == match_id))).all()
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}
    rounds = (await db.execute(select(RoundInstance).where(RoundInstance.match_id == match_id)
                               .order_by(RoundInstance.created_at))).scalars().all()
    aid = (await db.execute(select(MatchHouseState).where(MatchHouseState.match_id == match_id))).scalars().all()
    # 本场用的题库（大类）：由本题单里实际冻结的题反推，而不是新加一列。
    # 为什么要让主持人看得到：日常题库与竞赛题库都能办赛，开赛后"这场到底抽的哪一边"
    # 必须一眼可查；反推比新增字段少一次迁移，而且天然不会与题单不一致。
    pool_rows = (await db.execute(
        select(Question.pool, func.count())
        .join(MatchQuestion, MatchQuestion.question_id == Question.id)
        .where(MatchQuestion.match_id == match_id)
        .group_by(Question.pool)
    )).all()
    pools_present = [{"pool": p, "label": service.pool_label(p), "count": int(n)}
                     for p, n in sorted(pool_rows, key=lambda x: -x[1])]

    def _t(dt):
        """后台字段也必须走统一口径：naive-UTC 读出来标 Z，前端 new Date() 才不会按本机时区猜。"""
        return iso_z(dt)

    return {
        "match": {"id": m.id, "title": m.title, "mode": m.mode, "status": m.status,
                  "state_version": m.state_version, "planned_questions": m.planned_questions,
                  "fixed_k": m.fixed_k, "min_valid_questions": m.min_valid_questions,
                  "display_seconds": m.display_seconds, "answer_seconds": m.answer_seconds,
                  "interlude_seconds": m.interlude_seconds, "aid_enabled": m.aid_enabled,
                  "aid_window_seconds": m.aid_window_seconds,
                  "question_source": m.question_source,
                  # 题单实际来自哪个题库（见上方 pools_present 的说明）
                  "question_pool": pools_present[0]["pool"] if pools_present else None,
                  "question_pool_label": pools_present[0]["label"] if pools_present else None,
                  "question_pools": pools_present,
                  "competition_mode": getattr(m, "competition_mode", None) or "shared_buzz",
                  "questions_per_house": getattr(m, "questions_per_house", None),
                  "weight_time": getattr(m, "weight_time", 0),
                  "scoring_method": m.scoring_method, "scoring_top_n": m.scoring_top_n,
                  "scoring_mode": getattr(m, "scoring_mode", None) or "pure",
                  "weight_accuracy": getattr(m, "weight_accuracy", 0),
                  "weight_speed": getattr(m, "weight_speed", 0),
                  "weight_participation": getattr(m, "weight_participation", 0),
                  # 抢答：读题窗口为 0。前端据此决定是否画"读题"阶段。
                  "buzz": int(m.display_seconds or 0) <= 0,
                  "scoring_note": (rules.scoring_mode_note(getattr(m, "scoring_mode", "pure"),
                                                           getattr(m, "weight_accuracy", 0),
                                                           (getattr(m, "weight_time", 0)
                                                            if (getattr(m, "competition_mode", "shared_buzz")
                                                                == "relay")
                                                            else getattr(m, "weight_speed", 0)),
                                                           getattr(m, "competition_mode", "shared_buzz"))
                                   if (getattr(m, "scoring_mode", "pure") == "composite")
                                   else rules.scoring_note(m.scoring_method, m.scoring_top_n or 5)),
                  "difficulty_mix": m.difficulty_mix,
                  "spectator_mode": getattr(m, "spectator_mode", None) or "mirror",
                  "houses_scope": m.houses_scope,
                  "start_at": _t(m.start_at),
                  "ceremony_start_at": _t(m.ceremony_start_at), "reveal_at": _t(m.reveal_at)},
        "roster": [{"entry_id": e.id, "username": u.username, "role": u.role,
                    "house": houses[e.house_id].name_cn if e.house_id in houses else None,
                    "house_key": houses[e.house_id].key if e.house_id in houses else None,
                    "alias": e.display_alias, "is_captain": e.is_captain,
                    "checked_in": e.checked_in_at is not None,
                    "scoring_status": e.scoring_status,
                    "exclusion_reason": e.exclusion_reason} for e, u in entries],
        "rounds": [{"id": r.id, "status": r.status, "attempt_no": r.attempt_no,
                    "display_at": _t(r.display_at), "open_at": _t(r.open_at),
                    "deadline_at": _t(r.deadline_at), "void_reason": r.void_reason} for r in rounds],
        "aid": [{"house": houses[s.house_id].name_cn if s.house_id in houses else None,
                 "house_key": houses[s.house_id].key if s.house_id in houses else None,
                 "remaining": s.aid_remaining} for s in aid],
    }


class CommandIn(BaseModel):
    command_id: str = Field(min_length=8, max_length=64)
    action: str = Field(min_length=3, max_length=32)
    expected_state_version: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/admin/matches/{match_id}/commands")
async def run_command(match_id: str, body: CommandIn, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    m = await _match_or_404(db, match_id)
    return await service.execute_command(db, admin, m, body.command_id, body.action,
                                         body.expected_state_version, body.payload)


@router.post("/admin/matches/{match_id}/captain")
async def set_captain(match_id: str, body: dict, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    username = (body or {}).get("username")
    if not username:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "需要 username。")
    return await service.set_captain(db, admin, match_id, username)


@router.get("/admin/matches/{match_id}/events")
async def match_events(match_id: str, limit: int = 100, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(MatchEvent).where(MatchEvent.match_id == match_id)
                             .order_by(MatchEvent.id.desc()).limit(limit))).scalars().all()
    return {"items": [{"id": e.id, "type": e.type, "state_version": e.state_version,
                       "payload": e.payload, "created_at": e.created_at} for e in rows]}


@router.get("/admin/matches/{match_id}/commands")
async def match_commands(match_id: str, limit: int = 100, admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(AdminCommand).where(AdminCommand.match_id == match_id)
                             .order_by(AdminCommand.created_at.desc()).limit(limit))).scalars().all()
    return {"items": [{"command_id": c.command_id, "action": c.action, "result": c.result,
                       "error_code": c.error_code, "expected_state_version": c.expected_state_version,
                       "created_at": c.created_at} for c in rows]}
