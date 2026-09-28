"""评分与结果发布（§15.3、§10.4）。

- 读取冻结名单、有效回合（closed）与持久答案，生成不可变成绩版本。
- 重试结算不产生重复版本（version_no 递增，赛后更正必须走 recalculate 命令并留审计）。
- 排名用精确分数比较（rules.Fraction.cmp），显示才保留两位小数。
"""
import statistics
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import new_id
from app.db.base import as_utc, iso_z, utcnow
from app.db.models import (
    AidRequest,
    Answer,
    House,
    HouseResult,
    HouseTimeResult,
    Match,
    MatchEntry,
    MatchEvent,
    MatchQuestion,
    ParticipantResult,
    QuestionVersion,
    ResultVersion,
    RoundInstance,
    ROUND_CLOSED,
)
from app.modules.audit.service import audit
from app.modules.competitions.rules import (
    Fraction,
    HouseStanding,
    accuracy_of,
    composite_score,
    house_score,
    is_official_result,
    min_valid_questions,
    normalize_best_to_100,
    participant_score,
    participation_rate,
    rank_standings,
    relay_completion_ms,
    relay_time_factor,
    round_speed_factor,
    score_by_method,
    scoring_mode_note,
    scoring_note,
)


async def _round_correct_map(db: AsyncSession, round_ids: list[str]) -> dict[str, str]:
    """round_instance_id → 正确 option_id（来自冻结的题目版本）。"""
    if not round_ids:
        return {}
    rows = (await db.execute(
        select(RoundInstance.id, QuestionVersion.correct_option_id)
        .join(MatchQuestion, MatchQuestion.id == RoundInstance.match_question_id)
        .join(QuestionVersion, QuestionVersion.id == MatchQuestion.version_id)
        .where(RoundInstance.id.in_(round_ids))
    )).all()
    return {rid: correct for rid, correct in rows}


async def calculate_result(db: AsyncSession, match: Match, actor_id: str | None,
                           exclusions: dict[str, str] | None = None,
                           is_correction: bool = False, note: str | None = None) -> ResultVersion:
    # 有效题集合 = 本场所有已正常关闭的回合（主+备用，统一分母 §10.3）
    rounds = (await db.execute(
        select(RoundInstance)
        .where(RoundInstance.match_id == match.id, RoundInstance.status == ROUND_CLOSED)
    )).scalars().all()
    valid_round_ids = [r.id for r in rounds]
    valid_count = len(valid_round_ids)

    comp_mode = getattr(match, "competition_mode", None) or "shared_buzz"
    relay = comp_mode == "relay"

    # 接力赛：每个回合只归一个学院；把"回合 → 归属学院"和"该回合的题目位置"记下来，
    # 用来算每院的题量、每题用时与总用时。
    round_house: dict[str, str | None] = {}
    round_position: dict[str, int | None] = {}
    for r in rounds:
        scope = getattr(r, "house_scope", None)
        round_house[r.id] = (scope[0] if scope else None)
        mq = await db.get(MatchQuestion, r.match_question_id)
        round_position[r.id] = None if (mq is None or mq.is_backup) else mq.position
    # 每院的"有效题数"在接力赛里是本院自己的题量（不是全场题量）
    house_valid_count: dict[str, int] = defaultdict(int)
    if relay:
        for rid in valid_round_ids:
            if round_house.get(rid) is None:
                continue
            house_valid_count[round_house[rid]] += 1

    # 赛后更正：把剔除落到 entry 上（公开口径 + 审计，§10.3）
    if exclusions:
        for entry_id, reason in exclusions.items():
            entry = await db.get(MatchEntry, entry_id)
            if entry is not None and entry.match_id == match.id:
                entry.scoring_status = "excluded"
                entry.exclusion_reason = reason

    entries = (await db.execute(
        select(MatchEntry).where(MatchEntry.match_id == match.id,
                                 MatchEntry.scoring_status == "active")
    )).scalars().all()
    entry_ids = {e.id for e in entries}

    answers = (await db.execute(select(Answer).where(Answer.match_id == match.id))).scalars().all()
    correct_map = await _round_correct_map(db, valid_round_ids)
    valid_set = set(valid_round_ids)

    per_entry_correct: dict[str, int] = defaultdict(int)
    per_entry_answered: dict[str, int] = defaultdict(int)
    # 抢答速度：逐份答卷的用时（elapsed_ms 由服务端在受理时算，不信客户端上报）
    per_entry_speed: dict[str, Fraction] = defaultdict(lambda: Fraction(0, 1))
    per_entry_speed_n: dict[str, int] = defaultdict(int)
    fastest_by_round: dict[str, int] = {}
    for a in answers:
        if a.round_instance_id in valid_set and a.entry_id in entry_ids:
            per_entry_answered[a.entry_id] += 1
            if correct_map.get(a.round_instance_id) == a.option_id:
                per_entry_correct[a.entry_id] += 1
                if a.elapsed_ms is not None and a.elapsed_ms >= 0:
                    cur = fastest_by_round.get(a.round_instance_id)
                    if cur is None or a.elapsed_ms < cur:
                        fastest_by_round[a.round_instance_id] = a.elapsed_ms
    # 速度系数要等全场最快者算完才能定，因此第二轮再遍历一次。
    # 每次累加后**立刻约分**：分母相乘会让数值迅速膨胀到超出 PostgreSQL int32。
    for a in answers:
        if a.round_instance_id in valid_set and a.entry_id in entry_ids \
                and correct_map.get(a.round_instance_id) == a.option_id:
            factor = round_speed_factor(a.elapsed_ms, fastest_by_round.get(a.round_instance_id))
            cur = per_entry_speed[a.entry_id]
            per_entry_speed[a.entry_id] = Fraction(
                cur.num * factor.den + factor.num * cur.den,
                cur.den * factor.den).reduced()
            per_entry_speed_n[a.entry_id] += 1

    # 接力赛的手感速度：先算**每道题最快被本院解出的时刻**，再求和得总用时。
    #
    # 为什么不是"最后一秒（max）"：接力赛里一旦有人抢答正确，这道题对本院就结束了——
    # 后面的人不会再去答同一道题。用 max 会把"有人抢到了"误算成"全队都答完才结束"，
    # 越多人参赛时间反而越长，与现场事实相反。
    # 每院在各回合都取自己的最快一份答卷，求和即为"做完全部题目的净用时"。
    per_round_fastest: dict[tuple[str | None, str], int] = {}
    for a in answers:
        if a.round_instance_id not in valid_set or a.entry_id not in entry_ids:
            continue
        if a.elapsed_ms is None or a.elapsed_ms < 0:
            continue
        key = (round_house.get(a.round_instance_id), a.round_instance_id)
        cur = per_round_fastest.get(key)
        if cur is None or a.elapsed_ms < cur:
            per_round_fastest[key] = a.elapsed_ms
    house_total_ms: dict[str, int] = defaultdict(int)
    house_answered_rounds: dict[str, int] = defaultdict(int)
    for (hkey, _rid), ms in per_round_fastest.items():
        if hkey is None:
            continue
        house_total_ms[hkey] += ms
        house_answered_rounds[hkey] += 1

    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}
    last = (await db.execute(
        select(ResultVersion.version_no).where(ResultVersion.match_id == match.id)
        .order_by(ResultVersion.version_no.desc()).limit(1)
    )).first()
    next_no = (last[0] + 1) if last else 1

    effective_min_valid = match.min_valid_questions or min_valid_questions(match.planned_questions)

    rv = ResultVersion(id=new_id(), match_id=match.id, version_no=next_no,
                       valid_question_count=valid_count,
                       is_demo=match.title.startswith("[DEMO]"),
                       notes=note, calculated_by=actor_id)
    db.add(rv)
    await db.flush()

    # 评分模式与权重（赛前冻结在 match 上；见 models 常量区说明）
    score_mode = getattr(match, "scoring_mode", None) or "pure"
    w_acc = int(getattr(match, "weight_accuracy", 0) or 0)
    w_speed = int(getattr(match, "weight_speed", 0) or 0)
    # 接力赛的"速度项"是**时间权重**（weight_time），不是抢答的 weight_speed。
    # 两个字段分开存是因为语义不同：抢答按"同一题最快者"折算，接力按"总用时最快者"折算；
    # 用错字段会让后台设的时间权重完全不生效（实测：设 0 仍然按时间算分）。
    w_time = int(getattr(match, "weight_time", 0) or 0)
    if relay:
        w_speed = w_time
    # 参与度权重：缺省 0 = 不计入。启用后按"本院实际上场人数 / 冻结名单人数"加权。
    w_part = int(getattr(match, "weight_participation", 0) or 0)
    composite = score_mode == "composite"

    # 个人成绩：pure = 100 × 答对 ÷ 有效题数；composite = 正确率/速度/参与度按权重合成。
    # 注意接力赛：**个人分的分母是本院题量**，不是全场题量——每个学院只有自己那几道题，
    # 用全场题量（例如 20）去除，一个把本院 5 题全做对的人也只能拿 25 分，明显不成立。
    def _valid_for(member_house_id: str) -> int:
        if not relay:
            return valid_count
        key = houses[member_house_id].key if member_house_id in houses else None
        return house_valid_count.get(key, valid_count) if key else valid_count

    # 个人参与度：这个人在这场里到底答过题没有（0 或 1）。
    # 个人维度只有"答没答"这一种事实，用比例反而会奖励"瞎按很多次"，因此取二值。
    def _member_participation(entry_id: str) -> Fraction:
        return Fraction(1, 1) if per_entry_answered.get(entry_id, 0) > 0 else Fraction(0, 1)

    member_composite: dict[str, Fraction] = {}
    for e in entries:
        correct = per_entry_correct.get(e.id, 0)
        answered = per_entry_answered.get(e.id, 0)
        vcount = _valid_for(e.house_id)
        frac = (composite_score(correct, vcount, per_entry_speed[e.id],
                                per_entry_speed_n[e.id], w_acc, w_speed,
                                _member_participation(e.id), w_part)
                if composite else participant_score(correct, vcount))
        member_composite[e.id] = frac
        db.add(ParticipantResult(result_version_id=rv.id, entry_id=e.id, user_id=e.user_id,
                                 house_id=e.house_id, correct_count=correct, answered_count=answered,
                                 valid_question_count=vcount,
                                 score_num=frac.num, score_den=frac.den))

    # House 聚合（未答计零，中退不缩分母；故障剔除已落在 entry 上）
    # 评分标准由**赛前冻结**的 match.scoring_method 决定，四院共用同一套（否则不可比）。
    by_house: dict[str, list[MatchEntry]] = defaultdict(list)
    for e in entries:
        by_house[e.house_id].append(e)
    method = getattr(match, "scoring_method", None) or "avg"
    top_n = getattr(match, "scoring_top_n", None) or 5
    # 接力赛的时间基准：四院里最快做完自己那份题的学院 = 100% 时间分
    fastest_house_ms = min(house_total_ms.values()) if house_total_ms else None
    relay_time: dict[str, dict] = {}
    standings: list[HouseStanding] = []
    # **每个在册 House 都要有一行**，哪怕它一个人都没来。
    #
    # 这里曾经只遍历 by_house（有参赛者的学院），于是"某院无人签到"时结果载荷里
    # 直接少一行：公开结果页少一个学院、大屏矩阵缺一列，主机看到 empty houses 也无法
    # 判断是"真没人"还是"数据坏了"。未到的学院记 0 分并标注不满足最低人数，
    # 与"未答计零"是同一条规则，也才符合"四院同规则"（§10.3）。
    for hid, h in houses.items():
        members = by_house.get(hid, [])
        correct_total = sum(per_entry_correct.get(m.id, 0) for m in members)
        member_correct = [per_entry_correct.get(m.id, 0) for m in members]
        answered_total = sum(per_entry_answered.get(m.id, 0) for m in members)
        hv = house_valid_count.get(h.key, valid_count) if relay else valid_count
        if relay:
            # 接力赛的学院分 = (w_acc × 本院正确率 + w_time × 时间系数) ÷ (w_acc + w_time) × 100
            #
            # 正确率的分母是**本院题量**（四院题量相同，因此仍然可比）；
            # 时间系数以"最快完成的学院"为基准（fastest / ours），与抢答的逐题速度同构。
            total_ms = house_total_ms.get(h.key)
            answered_rounds = house_answered_rounds.get(h.key, 0)
            t_factor = relay_time_factor(total_ms, fastest_house_ms)
            relay_time[h.key] = {
                "total_ms": total_ms,
                "per_question_ms": (round(total_ms / answered_rounds) if total_ms and answered_rounds else None),
                "answered_rounds": answered_rounds,
                "expected_rounds": hv,
                "time_factor": t_factor.display(),
                "completed": answered_rounds >= hv and hv > 0,
            }
            if composite:
                # 正确率按**人题量**：本院每个人每回合只答一次，因此分母 = 人数 × 本院题量。
                # 未作答记零（与"未答计零"一致），不做"只算答过的人"的宽松处理。
                acc = accuracy_of(correct_total, hv * max(len(members), 1))
                # 学院参与度 = 本院**确实提交过**的人数 ÷ 冻结名单人数。
                # 注意用 members（冻结名单）作分母而不是"场上还有人"：中退的人不该让参与度回升。
                p_rate = participation_rate(
                    sum(1 for m in members if per_entry_answered.get(m.id, 0) > 0), len(members))
                num = (w_acc * acc.num * t_factor.den * p_rate.den
                       + w_speed * t_factor.num * acc.den * p_rate.den
                       + w_part * p_rate.num * acc.den * t_factor.den) * 100
                den = max((w_acc + w_speed + w_part) * acc.den * t_factor.den * p_rate.den, 1)
                frac = Fraction(num, den).quantized()
            else:
                frac = Fraction(correct_total * 100, max(hv * max(len(members), 1), 1))
        elif composite:
            # 学院综合分 = (w_acc×本院正确率 + w_speed×本院平均速度系数 + w_part×本院参与度)
            #              ÷ 权重之和 × 100
            #
            # 为什么**不能**用"本院成员个人分的算术平均"（这是原来的实现，也是本次实测发现的缺陷）：
            # 个人参与度是二值的（这个人答没答），把它平均起来恰好等于"本院参与人数比例"，
            # 看起来等价、实际上被正确率项**重复放大**了 —— 未参与的人既拉低了正确率，
            # 又在个人分里各扣一次参与度。实测：3 人只 1 人参与时，
            # 打开参与度权重前后学院分完全不变（33.33 → 33.33），也就是这一维在抢答赛里形同不存在。
            # 按学院一层直接合成后，参与度只作用一次，口径也更好向学生解释：
            # "本院正确率 × 权重 + 本院速度 × 权重 + 本院参与度 × 权重"。
            if members:
                acc = accuracy_of(correct_total, valid_count * len(members))
                # 速度只在**答对的人**身上取平均：没答对的人在正确率项已经扣过，
                # 再按 0 计入速度项等于罚两次（实测 tiger 因此拿 48.72 而不是 66.67）。
                # 这条口径与 rules.composite_score 的注释一致："平均速度系数只在答对的题上取平均"。
                speed_sum = Fraction(0, 1)
                speed_n = 0
                for m in members:
                    if per_entry_speed_n[m.id] <= 0:
                        continue
                    cur = per_entry_speed[m.id]
                    speed_sum = Fraction(cur.num * speed_sum.den + speed_sum.num * cur.den,
                                         cur.den * speed_sum.den).reduced()
                    speed_n += per_entry_speed_n[m.id]
                avg_speed = (Fraction(speed_sum.num, speed_sum.den * speed_n).reduced()
                             if speed_n > 0 else Fraction(0, 1))
                p_rate = participation_rate(
                    sum(1 for m in members if per_entry_answered.get(m.id, 0) > 0), len(members))
                num = (w_acc * acc.num * avg_speed.den * p_rate.den
                       + w_speed * avg_speed.num * acc.den * p_rate.den
                       + w_part * p_rate.num * acc.den * avg_speed.den) * 100
                den = max((w_acc + w_speed + w_part) * acc.den * avg_speed.den * p_rate.den, 1)
                frac = Fraction(num, den).quantized()
            else:
                frac = Fraction(0, 1)
        else:
            frac = score_by_method(method, member_correct, valid_count,
                                   answered_total=answered_total, top_n=top_n)
        meets = bool(members) and len(members) >= match.min_house_size and (
            match.mode != "fixed_k" or len(members) == (match.fixed_k or len(members)))
        standings.append(HouseStanding(house_id=hid, house_key=h.key,
                                       house_name=h.name_cn,
                                       participants=len(members), correct_total=correct_total,
                                       score=frac, meets_minimum=meets))
    # 总分制：原始分与人数同量纲，必须归一化到「榜首 = 100」才与其它口径可比、跨场可比。
    # 归一化需要看到四院全貌，因此放在这里而不是 score_by_method 里。
    if method == "total" and standings:
        norm = normalize_best_to_100([s.score for s in standings])
        for s, n in zip(standings, norm):
            s.score = n
    ranked = rank_standings(standings)
    official, reason = is_official_result(valid_count, effective_min_valid,
                                          [s.meets_minimum for s in ranked])
    for s in ranked:
        db.add(HouseResult(id=new_id(), result_version_id=rv.id, house_id=s.house_id,
                           house_key_snapshot=s.house_key, house_name_snapshot=s.house_name,
                           participant_count=s.participants, correct_total=s.correct_total,
                           score_num=s.score.num, score_den=s.score.den,
                           rank=s.rank, is_tie=s.is_tie, meets_minimum=s.meets_minimum))
    rv.is_official = official
    if reason:
        rv.notes = ((rv.notes + "；") if rv.notes else "") + f"不满足正式排名条件：{reason}"

    await audit(db, actor_id,
                "results.recalculate" if is_correction else "results.calculate",
                "match", match.id,
                {"version_no": next_no, "valid_questions": valid_count, "official": official})
    # 接力赛的用时指标落**结构化列**（不塞进 notes：notes 是给人看的中文说明，
    # 混进机器数据会让结果页把 JSON 当说明印出来）
    if relay:
        for key, info in relay_time.items():
            hrow = next((h for h in houses.values() if h.key == key), None)
            if hrow is None:
                continue
            db.add(HouseTimeResult(
                id=new_id(), result_version_id=rv.id, house_id=hrow.id, house_key_snapshot=key,
                total_ms=info["total_ms"], per_question_ms=info["per_question_ms"],
                answered_rounds=info["answered_rounds"], expected_rounds=info["expected_rounds"],
                time_factor_permille=int(round(float(info["time_factor"]) * 1000)),
                completed=bool(info["completed"])))
    return rv


async def publish_reveal(db: AsyncSession, match: Match, now=None) -> ResultVersion | None:
    """到 reveal_at 发布结果版本（§15.4）。由控制器 tick 触发，幂等；不依赖一次性 setTimeout。"""
    now = now or as_utc(utcnow())
    reveal_at = as_utc(match.reveal_at)
    if match.status != "reveal_scheduled" or reveal_at is None or now < reveal_at:
        return None
    rv = (await db.execute(
        select(ResultVersion).where(ResultVersion.match_id == match.id)
        .order_by(ResultVersion.version_no.desc()).limit(1)
    )).scalar_one_or_none()
    if rv is None:
        return None
    match.status = "revealed"
    match.published_result_version_id = rv.id
    rv.published_at = utcnow()
    match.state_version += 1
    db.add(MatchEvent(match_id=match.id, state_version=match.state_version,
                      type="result.published",
                      payload={"result_version_id": rv.id, "version_no": rv.version_no}))
    await audit(db, None, "results.publish", "match", match.id, {"version_no": rv.version_no})
    return rv


async def latest_calculated_version(db: AsyncSession, match: Match) -> ResultVersion | None:
    return (await db.execute(
        select(ResultVersion).where(ResultVersion.match_id == match.id)
        .order_by(ResultVersion.version_no.desc()).limit(1)
    )).scalar_one_or_none()


async def results_payload(db: AsyncSession, match: Match, rv: ResultVersion) -> dict:
    """公开安全的结果载荷（§8）：匿名个人分布、口径标注、无姓名无账号。"""
    # 口径必须与 calculate_result 里实际用的那一套一致：这里重新从 match 读，
    # 而不是依赖上游变量（曾经因为引用了另一个函数里的局部变量而 500）。
    method = getattr(match, "scoring_method", None) or "avg"
    top_n = getattr(match, "scoring_top_n", None) or 5
    score_mode = getattr(match, "scoring_mode", None) or "pure"
    w_acc = int(getattr(match, "weight_accuracy", 0) or 0)
    w_speed = int(getattr(match, "weight_speed", 0) or 0)
    w_part = int(getattr(match, "weight_participation", 0) or 0)
    hrs = (await db.execute(
        select(HouseResult).where(HouseResult.result_version_id == rv.id)
        .order_by(HouseResult.rank, HouseResult.house_id)
    )).scalars().all()
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}
    participants = (await db.execute(
        select(ParticipantResult).where(ParticipantResult.result_version_id == rv.id)
    )).scalars().all()
    dist: dict[str, list[float]] = defaultdict(list)
    for p in participants:
        dist[p.house_id].append(participant_score(p.correct_count, p.valid_question_count).display())

    # 速度统计：统一读题起点(open_at)到服务端接收（§8）；仅描述，不参与计分
    answer_rows = (await db.execute(
        select(Answer.option_id, Answer.received_at, Answer.round_instance_id)
        .where(Answer.match_id == match.id)
    )).all()
    round_rows = (await db.execute(
        select(RoundInstance.id, RoundInstance.open_at).where(RoundInstance.match_id == match.id)
    )).all()
    round_open = {rid: as_utc(opened) for rid, opened in round_rows}
    correct_map = await _round_correct_map(db, list(round_open.keys()))
    times = []
    for option_id, received_at, rid in answer_rows:
        opened = round_open.get(rid)
        if opened is None or correct_map.get(rid) != option_id:
            continue
        recv = as_utc(received_at)
        if opened is not None and recv is not None:
            times.append((recv - opened).total_seconds())
    speed = None
    if times:
        speed = {"median_s": round(statistics.median(times), 1),
                 "sample_size": len(times),
                 "note": ("按本场抢答用时统计（作答开放到服务端接收）；"
                          if score_mode != "composite" else
                          "按本场抢答用时统计（作答开放到服务端接收），并以同一题最快者为基准折算速度分计入总评。")}

    aid_rows = (await db.execute(
        select(AidRequest).where(AidRequest.match_id == match.id)
    )).scalars().all()
    aid_used: dict[str, int] = defaultdict(int)
    for a in aid_rows:
        if a.status in ("answered", "expired"):
            aid_used[a.house_id] += 1

    # 接力赛的用时成绩（随版本冻结；抢答赛为空）
    time_rows = (await db.execute(
        select(HouseTimeResult).where(HouseTimeResult.result_version_id == rv.id)
    )).scalars().all()
    relay_times = {t.house_key_snapshot: {
        "total_ms": t.total_ms,
        "total_s": round(t.total_ms / 1000, 1) if t.total_ms is not None else None,
        "per_question_ms": t.per_question_ms,
        "per_question_s": round(t.per_question_ms / 1000, 1) if t.per_question_ms else None,
        "answered_rounds": t.answered_rounds,
        "expected_rounds": t.expected_rounds,
        "time_factor": round(t.time_factor_permille / 1000, 3),
        "completed": t.completed,
    } for t in time_rows}
    comp_mode = getattr(match, "competition_mode", None) or "shared_buzz"

    return {
        "match": {"id": match.id, "title": match.title, "status": match.status,
                  # 口径必须随结果一起给出：学生看到的每一分都要能追溯到算法
                  "question_source": getattr(match, "question_source", None) or "random",
                  "competition_mode": comp_mode,
                  "questions_per_house": getattr(match, "questions_per_house", None),
                  "scoring_method": method,
                  "scoring_top_n": top_n,
                  "scoring_mode": score_mode,
                  "weight_accuracy": w_acc,
                  "weight_speed": w_speed,
                  "weight_time": int(getattr(match, "weight_time", 0) or 0),
                  "weight_participation": w_part,
                  "scoring_note": (scoring_mode_note(score_mode, w_acc,
                                                     int(getattr(match, "weight_time", 0) or 0)
                                                     if comp_mode == "relay" else w_speed,
                                                     comp_mode, w_part)
                                   if score_mode == "composite"
                                   else scoring_note(method, top_n))},
        "result_version": rv.version_no,
        "published_at": iso_z(rv.published_at),
        "is_official": rv.is_official,
        "is_demo": rv.is_demo,
        "valid_question_count": rv.valid_question_count,
        "notes": rv.notes,
        "houses": [{
            "key": hr.house_key_snapshot,
            "name": hr.house_name_snapshot,
            "color_light": houses[hr.house_id].color_light if hr.house_id in houses else "#EDF2EE",
            "color_deep": houses[hr.house_id].color_deep if hr.house_id in houses else "#78877E",
            "score": Fraction(hr.score_num, hr.score_den).display(),
            "rank": hr.rank, "is_tie": hr.is_tie,
            "participants": hr.participant_count,
            "correct_total": hr.correct_total,
            "meets_minimum": hr.meets_minimum,
            "aid_used": aid_used.get(hr.house_id, 0),
            "distribution": sorted(dist.get(hr.house_id, []), reverse=True),
            # 接力赛：该院的总用时 / 每题用时（抢答赛为 None）
            "timing": relay_times.get(hr.house_key_snapshot),
        } for hr in hrs],
        "speed": speed,
    }
