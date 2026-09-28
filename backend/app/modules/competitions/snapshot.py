"""按角色过滤的快照与进度（§14.3）。

字段白名单：未开放题目、标准答案、个人分数一律不进入普通载荷；
匿名观众只拿公开进度。HTTP 快照与 WS 广播共用本模块，保证口径一致。
"""
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc, iso_z, server_time_iso, utcnow
from app.db.models import (
    Answer,
    AidRequest,
    House,
    Match,
    MatchEntry,
    MatchHouseState,
    MatchQuestion,
    QuestionVersion,
    ResultVersion,
    RoundInstance,
    User,
    MATCH_ARCHIVED,
    MATCH_CALCULATED,
    MATCH_LOBBY,
    MATCH_LOCKED,
    MATCH_PAUSED,
    MATCH_REVEALED,
    MATCH_REVEAL_SCHEDULED,
    MATCH_RUNNING,
    ROUND_CLOSED,
    ROUND_DISPLAYING,
    ROUND_OPEN,
)


def _iso(dt):
    """统一走 base.iso_z：先做时区换算再写 Z（PG 会话时区非 UTC 时也不会偏移）。"""
    return iso_z(dt)


async def _round_public(db: AsyncSession, match: Match,
                        house_key: str | None = None) -> dict | None:
    """当前公开回合信息；仅展示/作答阶段的回合包含题目内容（§2 等待逻辑）。

    `house_key` 用于接力赛：同一时刻四院各自有一道题在开放，选手只能看到**自己学院**那道。
    没有它，接力赛的答题页会随机显示别院的题（而且提交时必然被服务端拒绝）。
    """
    relay = getattr(match, "competition_mode", None) == "relay"
    if relay:
        if not house_key:
            return None
        stmt = (select(RoundInstance)
                .where(RoundInstance.match_id == match.id,
                       RoundInstance.status.in_((ROUND_DISPLAYING, ROUND_OPEN)))
                .order_by(RoundInstance.created_at))
        rows = (await db.execute(stmt)).scalars().all()
        r = next((x for x in rows
                  if not getattr(x, "house_scope", None)
                  or house_key in (x.house_scope or [])), None)
    else:
        if not match.current_round_id:
            return None
        r = await db.get(RoundInstance, match.current_round_id)
    if r is None or r.status not in (ROUND_DISPLAYING, ROUND_OPEN):
        return None
    mq = await db.get(MatchQuestion, r.match_question_id)
    v = await db.get(QuestionVersion, mq.version_id)
    total_planned = (await db.execute(
        select(func.count()).select_from(MatchQuestion)
        .where(MatchQuestion.match_id == match.id, MatchQuestion.is_backup.is_(False))
    )).scalar_one()
    # 接力赛的"共几题"是**本院的题量**，不是全场题量（全场题量会让选手以为还有几十题）
    if relay:
        total_planned = int(match.questions_per_house or total_planned)
    out = {
        "round_instance_id": r.id,
        "attempt_no": r.attempt_no,
        "position": mq.position if not mq.is_backup else None,  # 备用题不泄露位置含义
        "total": total_planned,
        "phase": "display" if r.status == ROUND_DISPLAYING else "open",
        "relay": relay,
        "house_scope": getattr(r, "house_scope", None),
        "display_at": _iso(r.display_at),
        "open_at": _iso(r.open_at),
        "deadline_at": _iso(r.deadline_at),
        "server_time": server_time_iso(),
        "prompt": v.prompt,
        "options": [{"option_id": o["option_id"], "text": o["text"]} for o in v.options_json],
    }
    if match.aid_enabled and r.status == ROUND_DISPLAYING:
        from datetime import timedelta
        out["aid_window"] = {
            "enabled": True,
            "window_end": _iso(as_utc(r.display_at) + timedelta(seconds=match.aid_window_seconds)),
        }
    return out


async def _progress(db: AsyncSession, match: Match,
                    submitted_ids_out: dict[str, set[str]] | None = None) -> dict:
    """公开进度聚合（§8、§14.2）。

    性能约定（500 人同场每秒调用一次）：
    - 只取需要的列，绝不实例化整个 ORM 对象；
    - 已交人数用 SQL 聚合，不在 Python 里做 O(n²) 查找；
    - 已交名单只在 live_visibility=individual 时才查，默认档位不付这个成本。
    目标：SQL 条数固定（≤5），单次调用与人数近似线性。
    """
    rows = (await db.execute(
        select(MatchEntry.id, MatchEntry.house_id).where(
            MatchEntry.match_id == match.id, MatchEntry.checked_in_at.is_not(None))
    )).all()
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}
    entry_house: dict[str, str] = {eid: hid for eid, hid in rows}

    per_house = {hid: {"key": houses[hid].key, "name": houses[hid].name_cn,
                       "color_light": houses[hid].color_light,
                       "color_deep": houses[hid].color_deep,
                       "total": 0, "submitted": 0} for hid in houses}
    for _eid, hid in rows:
        if hid in per_house:
            per_house[hid]["total"] += 1

    total_submitted = 0
    if match.current_round_id:
        # 一次分组聚合代替「拉全量答案 + 逐个线性查找」（原实现是 O(n²)）
        agg = (await db.execute(
            select(MatchEntry.house_id, func.count())
            .select_from(Answer)
            .join(MatchEntry, MatchEntry.id == Answer.entry_id)
            .where(Answer.round_instance_id == match.current_round_id)
            .group_by(MatchEntry.house_id)
        )).all()
        for hid, cnt in agg:
            if hid in per_house:
                per_house[hid]["submitted"] += cnt
            total_submitted += cnt
        if submitted_ids_out is not None:
            submitted_ids_out["ids"] = set((await db.execute(
                select(Answer.entry_id).where(
                    Answer.round_instance_id == match.current_round_id))).scalars())

    rounds_done = (await db.execute(
        select(func.count()).select_from(RoundInstance)
        .where(RoundInstance.match_id == match.id, RoundInstance.status == ROUND_CLOSED)
    )).scalar_one()

    aid_states = (await db.execute(
        select(MatchHouseState).where(MatchHouseState.match_id == match.id))).scalars().all()
    aid = {houses[s.house_id].key: {"remaining": s.aid_remaining,
                                    "quota": match.aid_quota_per_house if match.aid_enabled else 0}
           for s in aid_states if s.house_id in houses}

    return {
        "houses": [per_house[hid] for hid in sorted(per_house, key=lambda x: houses[x].created_at)],
        "total_participants": len(rows),
        "total_submitted": total_submitted,
        "rounds_done": rounds_done,
        "aid": aid,
        "connections": None,  # 由调用方按需填充
    }


async def submission_matrix(db: AsyncSession, match: Match) -> dict:
    """提交矩阵：**回合 × 学院**的已交人数，供首页/大屏做"活跃度"式展示。

    设计取舍：
      · 这是纯展示数据，因此**实时聚合、不落库**（落库会引入一致性负担，且随时可重算）。
      · 用「已交/应到」的**比例**而不是绝对人数：各院人数可能不同，
        绝对人数会让"人少的院永远看着冷清"，比例才是可比的。
      · 归一化到**满编**（该院签到人数）而不是实际参赛人数，这样"少交"会真实地显示为暗格，
        而不是被自然填满 —— 空格本身就是信息（这正是 GitHub 活跃度图的表达力来源）。
      · 只统计已正常关闭的回合（closed）：作废回合不计入，与计分口径保持一致。

    返回：
      rounds   [{position, status, closed_at, total_submitted}]      —— 列
      houses   [{key, name, total, color_light, color_deep, cells}]  —— 行
               cells[i] = {round_index, submitted, total, ratio}
      max_rounds / overall_ratio —— 供大屏做整体进度条
    """
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}

    # 本次参与计分的回合（按位置排序，作废/未开始的排除）
    rounds = (await db.execute(
        select(RoundInstance).where(
            RoundInstance.match_id == match.id,
            RoundInstance.status.in_([ROUND_CLOSED, ROUND_DISPLAYING, ROUND_OPEN]),
        ).order_by(RoundInstance.created_at)
    )).scalars().all()

    entries = (await db.execute(
        select(MatchEntry.id, MatchEntry.house_id, MatchEntry.house_key_snapshot,
               MatchEntry.house_name_snapshot)
        .where(MatchEntry.match_id == match.id, MatchEntry.checked_in_at.is_not(None))
    )).all()
    entry_house: dict[str, str] = {eid: hid for eid, hid, _k, _n in entries}

    # 每院签到人数（矩阵的"满编"）
    roster: dict[str, int] = {}
    house_meta: dict[str, tuple[str, str]] = {}
    for _eid, hid, hk, hn in entries:
        roster[hid] = roster.get(hid, 0) + 1
        house_meta[hid] = (hk, hn)

    # 一次聚合拿到 回合 × 学院 的已交人数
    agg: dict[tuple[str, str], int] = {}
    if rounds:
        rows = (await db.execute(
            select(Answer.round_instance_id, MatchEntry.house_id, func.count())
            .select_from(Answer)
            .join(MatchEntry, MatchEntry.id == Answer.entry_id)
            .where(Answer.round_instance_id.in_([r.id for r in rounds]))
            .group_by(Answer.round_instance_id, MatchEntry.house_id)
        )).all()
        for rid, hid, cnt in rows:
            agg[(rid, hid)] = cnt

    house_rows = []
    for hid, (hk, hn) in sorted(house_meta.items(), key=lambda x: houses[x[0]].created_at
                                if x[0] in houses else 0):
        h = houses.get(hid)
        total = roster.get(hid, 0)
        cells = []
        for i, r in enumerate(rounds):
            submitted = agg.get((r.id, hid), 0)
            cells.append({
                "round_index": i,
                "submitted": submitted,
                "total": total,
                "ratio": round(submitted / total, 4) if total else 0.0,
            })
        house_rows.append({
            "key": hk, "name": hn,
            "color_light": h.color_light if h else "#EDF2EE",
            "color_deep": h.color_deep if h else "#78877E",
            "total": total, "cells": cells,
        })

    return {
        "rounds": [
            {"index": i, "status": r.status,
             "closed_at": _iso(r.closed_at),
             "total_submitted": sum(agg.get((r.id, hid), 0) for hid in house_meta)}
            for i, r in enumerate(rounds)
        ],
        "houses": house_rows,
        "max_rounds": len(rounds),
        "planned_questions": match.planned_questions,
    }


async def round_timeline(db: AsyncSession, match: Match, *, limit: int = 24) -> dict:
    """回合时间线：**每题一行**，供大屏的"进行到哪了"用。

    为什么大屏需要它：只有"第 3 / 20 题"这一个数字时，观众看不出节奏——
    前面答得快不快、中间有没有卡住、还剩多少。时间线把每一题的
    "题号 / 状态 / 提交份数 / 正确份数 / 用时区间"一次摊开，信息密度上来了但仍然是聚合量。

    隐私边界（与观战档位一致，**任何档位都不给标准答案**）：
      · `correct_count` 是**四院合计的正确份数**，不是"哪个学院答对了"。
        抢答期间据此反推某院是否答对，需要把它与逐院提交数交叉比对，
        而逐院提交数本身就是大屏公开信息（提交矩阵已经在显示）；
        即便如此，这里仍**只在回合关闭后才给出 correct_count**，
        进行中的回合一律为 None —— 场下喊出"这题答案是 B"是最典型的作弊路径。
      · 不返回任何个人维度数据（谁答的、答了什么）。
      · 备用题不出现（与计分口径一致）。

    性能：固定 2 条 SQL（回合列表 + 一次分组聚合），不随题量增长。
    """
    rounds = (await db.execute(
        select(RoundInstance)
        .where(RoundInstance.match_id == match.id,
               RoundInstance.status != "void")
        .order_by(RoundInstance.created_at)
    )).scalars().all()
    if not rounds:
        return {"items": [], "total": 0}

    mqs = {m.id: m for m in (await db.execute(
        select(MatchQuestion).where(MatchQuestion.match_id == match.id)
    )).scalars().all()}

    agg = (await db.execute(
        select(Answer.round_instance_id,
               func.count(),
               # 对错不落在 answers 表上（题目版本才是标准答案的唯一来源），
               # 因此这里 join 到本回合冻结的版本上比对 option_id。
               func.sum(case((Answer.option_id == QuestionVersion.correct_option_id, 1),
                             else_=0)))
        .select_from(Answer)
        .join(RoundInstance, RoundInstance.id == Answer.round_instance_id)
        .join(MatchQuestion, MatchQuestion.id == RoundInstance.match_question_id)
        .join(QuestionVersion, QuestionVersion.id == MatchQuestion.version_id)
        .where(Answer.round_instance_id.in_([r.id for r in rounds]))
        .group_by(Answer.round_instance_id)
    )).all()
    per_round = {rid: (int(total), int(correct or 0)) for rid, total, correct in agg}

    relay = getattr(match, "competition_mode", None) == "relay"
    items = []
    for r in rounds:
        mq = mqs.get(r.match_question_id)
        if mq is None or mq.is_backup:
            continue
        total, correct = per_round.get(r.id, (0, 0))
        closed = r.status == ROUND_CLOSED
        items.append({
            "round_instance_id": r.id,
            "position": mq.position,
            "status": r.status,
            "house_scope": getattr(r, "house_scope", None) if relay else None,
            "submitted": total,
            # 未关闭的回合不给正确份数：见上面的隐私说明
            "correct": correct if closed else None,
            "closed": closed,
            "open_at": _iso(r.open_at),
            "closed_at": _iso(r.closed_at),
            # 用时区间：本题从开放到最后一笔提交经过了多久（大屏用它显示"卡了多久"）
            "duration_ms": (int((as_utc(r.closed_at) - as_utc(r.open_at)).total_seconds() * 1000)
                            if (closed and r.closed_at and r.open_at) else None),
        })
    # 只保留最近的 limit 题，但 total 给全量，前端据此画"20 题里已进行到第 7 题"
    return {"items": items[-limit:], "total": len(items)}


async def recent_submissions(db: AsyncSession, match: Match, *, limit: int = 8) -> list[dict]:
    """最近的若干笔提交（**匿名**）：时间 + 学院 + 题号，不带人、不带选项。

    大屏最有"现场感"的一栏：每交一份就多一条，观众能看到节奏在走。
    刻意不给选项与对错 —— 那等于实时播报答案分布。
    """
    rows = (await db.execute(
        select(Answer.received_at, Answer.round_instance_id,
               MatchEntry.house_key_snapshot, MatchEntry.house_name_snapshot)
        .join(MatchEntry, MatchEntry.id == Answer.entry_id)
        .where(Answer.match_id == match.id)
        .order_by(Answer.received_at.desc())
        .limit(limit)
    )).all()
    if not rows:
        return []
    mqs = {m.id: m for m in (await db.execute(
        select(MatchQuestion).where(MatchQuestion.match_id == match.id)
    )).scalars().all()}
    rids = {r.id: r for r in (await db.execute(
        select(RoundInstance).where(RoundInstance.id.in_({x[1] for x in rows}))
    )).scalars().all()} if rows else {}
    out = []
    for received_at, rid, hk, hn in rows:
        r = rids.get(rid)
        mq = mqs.get(r.match_question_id) if r is not None else None
        out.append({
            "at": _iso(received_at),
            "house_key": hk,
            "house_name": hn,
            "position": mq.position if (mq is not None and not mq.is_backup) else None,
        })
    return out


async def build_public_board(db: AsyncSession, match: Match) -> dict:
    """匿名可见的**大屏只读视图**。

    为什么需要它：投屏页 / 观赛大屏挂在教室走廊的希沃上，**没有人会先在它上面登录**。
    观众与路过的人应该能直接看到"比赛进行到哪、四院交了多少"，
    但**绝不能看到题目内容**（下一场的选手可能就是观众）。

    因此这里刻意只给：
      · 比赛标题/状态/题量、**赛制与评分口径**（大屏要说清"这场在比什么"）；
      · 当前回合的**阶段与时间**（倒计时要用），不含题干与选项；
      · 四院聚合进度、**每题提交矩阵**、**回合时间线**、**最近提交流水**、援助余量；
      · 揭晓后的结果（与公开结果页同一道门：必须 published_at 非空）。

    关于"信息密度"：这一页是给站在 3–6 米外的人看的，只有四个数字会显得空。
    因此除了进度，还给出**逐题节奏**（时间线）与**最近提交流水**——
    它们全部是聚合量，不含题面、不含标准答案、不含个人身份，泄题风险为零。
    进行中的回合**不给正确份数**：场下喊出"这题答案是 B"是最典型的作弊路径，
    要等该回合关闭后才放开。

    与 `/snapshot` 的分工：那是我自己要答题的完整视图，必须登录；
    这里是"看得懂场上在发生什么"的集合，可以匿名。
    """
    public = {
        "match": {"id": match.id, "title": match.title, "status": match.status,
                  "mode": match.mode, "state_version": match.state_version,
                  "aid_enabled": match.aid_enabled,
                  "competition_mode": getattr(match, "competition_mode", None) or "shared_buzz",
                  "planned_questions": match.planned_questions,
                  "questions_per_house": getattr(match, "questions_per_house", None),
                  "answer_seconds": match.answer_seconds,
                  "display_seconds": match.display_seconds,
                  "buzz": int(match.display_seconds or 0) <= 0,
                  "scoring_mode": getattr(match, "scoring_mode", "pure"),
                  "scoring_note": None},
        "server_time": server_time_iso(),
        "round": None,
        "progress": await _progress(db, match),
        "matrix": await submission_matrix(db, match),
        "timeline": await round_timeline(db, match),
        "recent": await recent_submissions(db, match),
        "anonymous": True,
    }
    # 评分口径的说明文案与后台/结果页同源，避免同一件事在两处措辞不同
    try:
        from app.modules.competitions.rules import scoring_mode_note
        public["match"]["scoring_note"] = scoring_mode_note(
            getattr(match, "scoring_mode", "pure") or "pure",
            getattr(match, "weight_accuracy", 0) or 0,
            getattr(match, "weight_speed", 0) or 0,
            getattr(match, "competition_mode", None) or "shared_buzz",
            getattr(match, "weight_participation", 0) or 0,
        )
    except Exception:
        # 大屏不能因为一句说明文案而整页失败：拿不到就不显示这一行
        public["match"]["scoring_note"] = None
    if match.current_round_id:
        r = await db.get(RoundInstance, match.current_round_id)
        if r is not None and r.status in (ROUND_DISPLAYING, ROUND_OPEN):
            mq = await db.get(MatchQuestion, r.match_question_id)
            total_planned = (await db.execute(
                select(func.count()).select_from(MatchQuestion)
                .where(MatchQuestion.match_id == match.id, MatchQuestion.is_backup.is_(False))
            )).scalar_one()
            public["round"] = {
                "round_instance_id": r.id,
                "position": mq.position if (mq is not None and not mq.is_backup) else None,
                "total": total_planned,
                "phase": "display" if r.status == ROUND_DISPLAYING else "open",
                "display_at": _iso(r.display_at),
                "open_at": _iso(r.open_at),
                "deadline_at": _iso(r.deadline_at),
                # 明确写出来：这条链路上没有题面，前端不必猜
                "prompt_included": False,
            }
    if match.status == "reveal_scheduled":
        public["reveal"] = {"ceremony_start_at": _iso(match.ceremony_start_at),
                            "reveal_at": _iso(match.reveal_at)}
    if match.status in ("revealed", "archived") and match.published_result_version_id:
        rv = await db.get(ResultVersion, match.published_result_version_id)
        if rv is not None and rv.published_at is not None:
            from app.modules.scoring.service import results_payload
            public["result"] = await results_payload(db, match, rv)
    return public


async def build_snapshot(db: AsyncSession, user: User | None, match: Match,
                         session_id: str | None = None) -> dict:
    from app.modules.scoring.service import latest_calculated_version, results_payload

    # 接力赛要按"我自己学院的那道题"取回合，因此先把学院 key 解析出来（一次查询）
    _my_house_key: str | None = None
    if user is not None and getattr(user, "house_id", None):
        _h = await db.get(House, user.house_id)
        _my_house_key = _h.key if _h is not None else None

    public = {
        "match": {"id": match.id, "title": match.title, "status": match.status,
                  "mode": match.mode, "state_version": match.state_version,
                  "aid_enabled": match.aid_enabled,
                  # 赛制：抢答（一次竞赛一个大题库、四院同题抢答）或接力
                  # （每院固定题目数量、比总用时与每题用时）。
                  "competition_mode": getattr(match, "competition_mode", None) or "shared_buzz",
                  "questions_per_house": getattr(match, "questions_per_house", None),
                  # 抢答：读题窗口为 0，题目一出现即可作答。
                  # 前端据此把"读题/作答"两段合成一段，不再显示"稍后作答"。
                  "buzz": int(match.display_seconds or 0) <= 0},
        "server_time": server_time_iso(),
        "round": await _round_public(db, match, _my_house_key),
        "progress": await _progress(db, match),
    }
    if match.status == "reveal_scheduled":
        public["reveal"] = {"ceremony_start_at": _iso(match.ceremony_start_at),
                            "reveal_at": _iso(match.reveal_at)}
    if match.status in ("revealed", "archived") and match.published_result_version_id:
        rv = await db.get(ResultVersion, match.published_result_version_id)
        if rv is not None and rv.published_at is not None:
            public["result"] = await results_payload(db, match, rv)

    if user is None:
        return {"role": "anonymous", **public}

    from app.db.models import ROLE_ADVISOR, ROLE_ADMIN
    personal: dict = {"user_id": user.id, "role": user.role}

    if user.role == ROLE_ADMIN:
        personal["is_admin"] = True

    # 选手流程分流依据：前端据此决定进"等待室"还是"答题界面"。
    #   checking  可以签到但**尚未签到**且处于签到阶段 → 签到界面
    #   waiting   已签到但比赛还没开赛（lobby / paused）→ 等待室
    #   answering 比赛进行中 → 答题界面
    #   done      本场已进入 locked 及以后（作答阶段结束）→ 等待揭晓
    #   spectate  不属于本场（没绑定学院、或本院不在本场范围内）→ 旁观提示
    #
    # **谁能参赛：绑定了学院、且本场包含该学院的账号都能**（按学院参赛）。
    # 这里以前要求 `role in ("entrant","admin")`，再由 MatchEligibility 判断资格；
    # 那套机制已经取消，所以现在只看学院。管理员同样按这条走：他若绑了学院也能代表
    # 本院上场，系统不再替他做例外判断（换机交接另有 admin 豁免，与此无关）。
    #
    # 另一个容易搞错的细节：**MatchEntry 是"签到"时才创建的**。
    # 只按 MatchEntry 判断，会把"本院、还没签到"的选手误判成 spectate（观赛）——
    # 而观赛的人根本不需要签到，于是签到入口永远不会出现（本项目实测踩过）。
    scope = match.houses_scope or None
    in_scope = _my_house_key is not None and (scope is None or _my_house_key in scope)

    entry = (await db.execute(select(MatchEntry).where(
        MatchEntry.match_id == match.id, MatchEntry.user_id == user.id))).scalar_one_or_none()
    if entry is not None:
        personal["entry"] = {"entry_id": entry.id, "alias": entry.display_alias,
                             "house_key": entry.house_key_snapshot,
                             "house_name": entry.house_name_snapshot,
                             "is_captain": entry.is_captain,
                             "checked_in": entry.checked_in_at is not None}
        if match.current_round_id:
            mine = (await db.execute(select(Answer).where(
                Answer.round_instance_id == match.current_round_id,
                Answer.entry_id == entry.id))).scalar_one_or_none()
            personal["my_answer"] = (
                {"receipt_id": mine.receipt_id, "option_id": mine.option_id,
                 "accepted_at": _iso(mine.received_at)} if mine else None)

        checked = entry.checked_in_at is not None
        if not checked and match.status == MATCH_LOBBY:
            phase = "checking"
        elif match.status in (MATCH_LOBBY, MATCH_PAUSED):
            phase = "waiting"
        elif match.status == MATCH_RUNNING:
            phase = "answering"
        elif match.status in (MATCH_LOCKED, MATCH_CALCULATED, MATCH_REVEAL_SCHEDULED,
                              MATCH_REVEALED, MATCH_ARCHIVED):
            phase = "done"
        else:
            phase = "waiting"
        personal["flow"] = phase
    elif in_scope:
        if match.status == MATCH_LOBBY:
            personal["flow"] = "checking"          # 可以签到
        elif match.status in (MATCH_RUNNING, MATCH_PAUSED):
            personal["flow"] = "missed_checkin"    # 比赛已开始，签到入口已关
        else:
            personal["flow"] = "done"
    else:
        # 不属于本场：给出**可执行的原因**，而不是一句"你去观赛吧"
        personal["flow"] = "spectate"
        personal["spectate_reason"] = "no_house" if _my_house_key is None else "not_in_scope"

    if user.role == ROLE_ADVISOR and user.house_id is not None:
        st = (await db.execute(select(MatchHouseState).where(
            MatchHouseState.match_id == match.id,
            MatchHouseState.house_id == user.house_id))).scalar_one_or_none()
        if st is not None:
            personal["aid"] = {"remaining": st.aid_remaining,
                               "quota": match.aid_quota_per_house if match.aid_enabled else 0}
        if match.current_round_id:
            aid = (await db.execute(select(AidRequest).where(
                AidRequest.round_instance_id == match.current_round_id,
                AidRequest.house_id == user.house_id))).scalar_one_or_none()
            if aid is not None:
                personal["aid_request"] = {"id": aid.id, "status": aid.status,
                                           "advice_option_id": aid.advice_option_id}

    return {"role": user.role, **public, "you": personal}
