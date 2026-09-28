"""题库管理（§6、§13.3）。

流程：导入草稿 → 校验与去重 → 审核 → 发布 →（由比赛模块抽题冻结）。
标准答案只在本模块管理端与评分服务中出现；练习/比赛普通接口不返回 correct 字段。
"""
import csv
import hashlib
import io
import json
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_admin
from app.core.errors import AppError, ErrorCode
from app.core.security import fingerprint, new_id
from app.db.base import iso_z
from app.db.models import (
    POOL_OFFICIAL, POOL_PRACTICE, MatchQuestion, Question, QuestionVersion, User,
)
from app.modules.audit.service import audit

router = APIRouter(prefix="/api/v1", tags=["questions"])

LETTERS = ["A", "B", "C", "D", "E", "F"]

# 控制台一页最多 200 题：题库到几千题时，不设上限的 size 会让一次列表请求把
# 整张表连同版本内容一起吐出来（SQLite 单文件库上会直接卡住管理页）。
MAX_PAGE_SIZE = 200
# 批量操作的 id 上限与分页上限一致：一次点选能覆盖的最大范围就是"肉眼能看全的一屏"。
MAX_BULK_IDS = 200

REVIEW_STATES = ("pending", "approved", "rejected")
QUESTION_STATUSES = ("draft", "published", "retired")

# 排序白名单：把列写死在映射里，而不是把请求参数拼进 SQL，
# 这样 SQLite 与 PostgreSQL 上都只可能落到这四个固定顺序上。
_SORT_ORDER: dict[str, list] = {
    "created_desc": [Question.created_at.desc(), Question.id.desc()],
    "created_asc": [Question.created_at.asc(), Question.id.asc()],
    "subject": [Question.subject.asc(), Question.created_at.desc(), Question.id.asc()],
    "difficulty": [Question.difficulty.asc(), Question.created_at.desc(), Question.id.asc()],
}


def normalize_prompt(prompt: str) -> str:
    return "".join(prompt.split()).lower()


class OptionIn(BaseModel):
    text: str = Field(min_length=1, max_length=255)


class QuestionIn(BaseModel):
    subject: str = Field(min_length=1, max_length=32)
    difficulty: int = Field(default=1, ge=1, le=3)
    language: str = "zh"
    pool: str = POOL_PRACTICE
    prompt: str = Field(min_length=3, max_length=2000)
    options: list[OptionIn] = Field(min_length=2, max_length=6)
    correct_index: int = Field(ge=0)
    explanation: str | None = None
    source: str | None = Field(default=None, max_length=255)
    # 只在 POST /admin/questions 上生效（见 create_question）：
    # 管理员手录一道题时，作者与审核人是同一个人，允许一次调用直接进池，
    # 省掉"录完再点一次审核"。默认 False，保持既有"录题即待审"的流程不变。
    approve: bool = False


def _option_ids(n: int) -> list[str]:
    return [f"o{i + 1}" for i in range(n)]


def question_version_out(q: Question, v: QuestionVersion, with_answer: bool) -> dict:
    out: dict[str, Any] = {
        "question_id": q.id, "version_id": v.id, "version_no": v.version_no,
        "subject": q.subject, "difficulty": q.difficulty, "language": q.language,
        "pool": q.pool, "status": q.status, "review_state": v.review_state,
        "prompt": v.prompt,
        "options": [{"option_id": o["option_id"], "text": o["text"]} for o in v.options_json],
        "source": v.source,
    }
    if with_answer:
        out["correct_option_id"] = v.correct_option_id
        out["explanation"] = v.explanation
    return out


async def _get_question_version(db: AsyncSession, question_id: str) -> tuple[Question, QuestionVersion] | None:
    q = await db.get(Question, question_id)
    if q is None or q.current_version_id is None:
        return None
    v = await db.get(QuestionVersion, q.current_version_id)
    if v is None:
        return None
    return q, v


def correct_index_of(v: QuestionVersion) -> int:
    """由 `correct_option_id`（形如 "o1"）推出 0 基下标，供控制台高亮正确项。

    为什么要"防御式"解析：correct_option_id 是字符串，历史数据/导入的 CSV 里
    可能残留 "O1"、"1"、空串之类的值。控制台只是展示，遇到脏数据退化到 0 即可；
    若直接 int(x[1:]) 抛异常，一道脏题就能让整个题库列表 500。
    """
    raw = (v.correct_option_id or "").strip()
    digits = ""
    for ch in reversed(raw):
        if not ch.isdigit():
            break
        digits = ch + digits
    if not digits:
        return 0
    index = int(digits) - 1
    return index if index >= 0 else 0


def _unique_ids(ids: list[str]) -> list[str]:
    """去重且保持首次出现顺序。

    批量接口里同一个 id 重复出现时只应处理一次：否则"更新了 2 道题"其实只有 1 道，
    控制台的提示会骗人；保持原顺序则让 skipped 的顺序与入参一致，便于前端逐条对应高亮。
    """
    return list(dict.fromkeys(ids))


async def _used_in_matches_map(db: AsyncSession, ids: list[str]) -> dict[str, int]:
    """整页题目一次性取回"被比赛引用次数"。

    为什么是聚合而不是逐行 count：控制台一页最多 200 题，逐行查就是 200 次数据库往返，
    题目被引用次数的查询次数会随页大小线性增长；这里固定 1 次 `GROUP BY question_id`，
    无论一页 20 题还是 200 题，查询数都不变。
    没被引用过的题不会出现在结果里，调用方用 `used.get(id, 0)` 兜 0。
    """
    unique = _unique_ids(ids)
    if not unique:
        return {}
    rows = (await db.execute(
        select(MatchQuestion.question_id, func.count())
        .where(MatchQuestion.question_id.in_(unique))
        .group_by(MatchQuestion.question_id)
    )).all()
    return {qid: int(cnt) for qid, cnt in rows}


def admin_question_out(q: Question, v: QuestionVersion, used_in_matches: int) -> dict:
    """控制台列表/详情的一项 = 既有版本输出 + 控制台需要的元数据。

    为什么不直接塞进 `question_version_out()`：那一个函数同时服务于创建/改题/审核/恢复，
    那几处既拿不到（也不该为此多查一次聚合）引用次数。把"引用次数"留在调用点上显式传入，
    写接口就不会平白多打一次 match_questions 聚合查询。
    `updated_at` 取**当前版本**的 created_at：改题是新版本落库，它就是这道题最近一次变更时间。
    """
    out = question_version_out(q, v, True)
    out.update({
        "created_at": iso_z(q.created_at),
        "updated_at": iso_z(v.created_at),
        "correct_index": correct_index_of(v),
        "used_in_matches": int(used_in_matches),
    })
    return out


async def _load_questions(db: AsyncSession, ids: list[str]) -> dict[str, Question]:
    """按 id 批量取题（一次 IN 查询）。

    批量接口最多收 200 个 id，逐个 `db.get()` 会变成 200 次往返；
    找不到的 id 由调用方按顺序补进 skipped，保证返回的 skipped 顺序与入参一致。
    """
    unique = _unique_ids(ids)
    if not unique:
        return {}
    rows = (await db.execute(select(Question).where(Question.id.in_(unique)))).scalars().all()
    return {q.id: q for q in rows}


async def _load_current_versions(db: AsyncSession, ids: list[str]) -> dict[str, tuple[Question, QuestionVersion]]:
    """批量取（题目 + 当前版本）；审核类操作必须同时拿到两者。

    没有当前版本的题不会出现在结果里（脏数据），调用方按"无法审核"处理。
    """
    unique = _unique_ids(ids)
    if not unique:
        return {}
    rows = (await db.execute(
        select(Question, QuestionVersion)
        .join(QuestionVersion, QuestionVersion.id == Question.current_version_id)
        .where(Question.id.in_(unique))
    )).all()
    return {q.id: (q, v) for q, v in rows}


def _empty_pool_stats() -> dict[str, int]:
    """单个题池的计数骨架：键必须齐全且为 0，控制台才不用做空值判断。"""
    return {"total": 0, "draft": 0, "published": 0, "retired": 0, "pending_review": 0}


@router.get("/admin/questions")
async def list_questions(pool: str | None = None, status: str | None = None,
                         subject: str | None = None, q: str | None = None,
                         difficulty: int | None = None, review_state: str | None = None,
                         sort: str = "created_desc", page: int = 1, size: int = 50,
                         _admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    """题库列表（管理端）。

    既有参数 pool/status/subject/page/size 语义与返回结构（{total,page,size,items}）不变，
    新增控制台需要的能力：模糊搜索 q、难度/审核态过滤、白名单排序，
    以及每题的引用次数 used_in_matches（整页一次聚合，见 _used_in_matches_map）。
    """
    if sort not in _SORT_ORDER:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       f"sort 必须是 {list(_SORT_ORDER)} 之一。")
    if difficulty is not None and difficulty not in (1, 2, 3):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "difficulty 必须在 1-3 之间。")
    if review_state is not None and review_state not in REVIEW_STATES:
        raise AppError(422, ErrorCode.VALIDATION_ERROR,
                       "review_state 必须是 pending/approved/rejected 之一。")
    # 收敛而不报错：旧调用方可能传 size=500，直接 422 会把它们已经能用的列表页打挂，
    # 静默夹到上限既保住了兼容，也堵住了"一次拉全表"的口子。
    size = max(1, min(size, MAX_PAGE_SIZE))
    page = max(1, page)

    stmt = select(Question, QuestionVersion).join(
        QuestionVersion, QuestionVersion.id == Question.current_version_id
    ).order_by(*_SORT_ORDER[sort])
    if pool:
        stmt = stmt.where(Question.pool == pool)
    if status:
        stmt = stmt.where(Question.status == status)
    if subject:
        stmt = stmt.where(Question.subject == subject)
    if difficulty is not None:
        stmt = stmt.where(Question.difficulty == difficulty)
    if review_state is not None:
        stmt = stmt.where(QuestionVersion.review_state == review_state)
    if q and q.strip():
        # 题干/学科/来源/解析任一命中即可：控制台只有一个搜索框，
        # 老师记不住"该搜题干还是搜来源"，四列都试一遍最省事。
        # 两侧都 lower()：SQLite 的 LIKE 对 ASCII 本来就不区分大小写，
        # 但 PostgreSQL 区分，统一 lower() 才能让两种后端行为一致。
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(
            func.lower(QuestionVersion.prompt).like(needle),
            func.lower(Question.subject).like(needle),
            func.lower(func.coalesce(QuestionVersion.source, "")).like(needle),
            func.lower(func.coalesce(QuestionVersion.explanation, "")).like(needle),
        ))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.limit(size).offset((page - 1) * size))).all()
    used = await _used_in_matches_map(db, [question.id for question, _ in rows])
    return {"total": total, "page": page, "size": size,
            "items": [admin_question_out(question, version, used.get(question.id, 0))
                      for question, version in rows]}


@router.get("/admin/questions/stats")
async def question_stats(_admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    """题库概览（控制台首屏大盘）。

    全部计数都在 SQL 里分组算完：进一次题库页就会调这个接口，
    在 Python 里遍历全部题目在几千题的库上会把首屏拖到秒级。
    pending_review / review_queue 只看**当前版本**的 review_state：
    历史版本的旧状态不代表这道题现在待审（改题后上一版还是 approved，但新版本是 pending）。
    """
    pools: dict[str, dict[str, int]] = {POOL_OFFICIAL: _empty_pool_stats(),
                                       POOL_PRACTICE: _empty_pool_stats()}
    pool_rows = (await db.execute(
        select(Question.pool, Question.status, QuestionVersion.review_state, func.count())
        .join(QuestionVersion, QuestionVersion.id == Question.current_version_id)
        .group_by(Question.pool, Question.status, QuestionVersion.review_state)
    )).all()
    for pool, q_status, review, cnt in pool_rows:
        bucket = pools.get(pool)
        if bucket is None:
            # 未知池（历史脏数据）不进结果：对外结构只承诺 official/practice 两个键
            continue
        bucket["total"] += int(cnt)
        if q_status in QUESTION_STATUSES:
            bucket[q_status] += int(cnt)
        if review == "pending":
            bucket["pending_review"] += int(cnt)

    review_queue = sum(bucket["pending_review"] for bucket in pools.values())

    # 重复题 = 同池内 prompt_hash 相同的"多出来的"题（一组 3 道记 2 道）。
    # 若改成"重复组数"，控制台上的数字会与"去重后能删掉几道"对不上，管理员会以为去重没生效。
    dup_rows = (await db.execute(
        select(Question.pool, Question.prompt_hash, func.count())
        .group_by(Question.pool, Question.prompt_hash)
        .having(func.count() > 1)
    )).all()
    duplicates = sum(int(cnt) - 1 for _, _, cnt in dup_rows)

    # 学科分面：一次 GROUP BY subject 同时出四种状态与两个池的计数，
    # 避免为每个学科再各查一次（学科数量会随题库增长）。
    subject_rows = (await db.execute(
        select(Question.subject,
               func.count(),
               func.sum(case((Question.status == "draft", 1), else_=0)),
               func.sum(case((Question.status == "published", 1), else_=0)),
               func.sum(case((Question.status == "retired", 1), else_=0)),
               func.sum(case((Question.pool == POOL_OFFICIAL, 1), else_=0)),
               func.sum(case((Question.pool == POOL_PRACTICE, 1), else_=0)))
        .group_by(Question.subject)
        .order_by(func.count().desc(), Question.subject.asc())
    )).all()
    subjects = [{"subject": name, "total": int(total), "draft": int(draft),
                 "published": int(published), "retired": int(retired),
                 "official": int(official), "practice": int(practice)}
                for name, total, draft, published, retired, official, practice in subject_rows]
    return {"pools": pools, "subjects": subjects,
            "review_queue": int(review_queue), "duplicates": int(duplicates)}


@router.post("/admin/questions")
async def create_question(body: QuestionIn, admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    """新建题目（版本 1）。

    `approve=True` 时直接落成"已通过 + 已发布"：管理员手录题时作者与审核人同体，
    否则每录一道都要再点一次审核才能进抽题池。默认仍是 pending/draft。
    """
    if body.pool not in (POOL_OFFICIAL, POOL_PRACTICE):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "pool 必须是 official/practice。")
    if body.correct_index >= len(body.options):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "correct_index 超出选项范围。")
    qid = new_id()
    ids = _option_ids(len(body.options))
    q = Question(id=qid, subject=body.subject, difficulty=body.difficulty, language=body.language,
                 pool=body.pool, status="published" if body.approve else "draft",
                 prompt_hash=normalize_prompt(body.prompt), created_by=admin.id)
    v = QuestionVersion(id=new_id(), question_id=qid, version_no=1, prompt=body.prompt,
                        options_json=[{"option_id": ids[i], "text": o.text} for i, o in enumerate(body.options)],
                        correct_option_id=ids[body.correct_index], explanation=body.explanation,
                        source=body.source, review_state="approved" if body.approve else "pending",
                        created_by=admin.id)
    db.add_all([q, v])
    q.current_version_id = v.id
    await audit(db, admin.id, "questions.create", "question", qid, {"pool": body.pool})
    await db.commit()
    return question_version_out(q, v, True)


@router.put("/admin/questions/{question_id}")
async def new_version(question_id: str, body: QuestionIn, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """改题 = 新建一版（历史版本全部保留，便于赛后复盘与申诉）。

    注意：body.approve 在这里**不生效**——新版本一律回到待审。
    否则"改一个错字顺带把新版本标成已通过"就能绕过审核直接进比赛题库。
    """
    got = await _get_question_version(db, question_id)
    if got is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "题目不存在。")
    q, latest = got
    if body.correct_index >= len(body.options):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "correct_index 超出选项范围。")
    max_no = latest.version_no
    ids = _option_ids(len(body.options))
    v = QuestionVersion(id=new_id(), question_id=question_id, version_no=max_no + 1, prompt=body.prompt,
                        options_json=[{"option_id": ids[i], "text": o.text} for i, o in enumerate(body.options)],
                        correct_option_id=ids[body.correct_index], explanation=body.explanation,
                        source=body.source, review_state="pending", created_by=admin.id)
    db.add(v)
    q.current_version_id = v.id
    q.status = "draft"
    q.subject, q.difficulty, q.language = body.subject, body.difficulty, body.language
    q.prompt_hash = normalize_prompt(body.prompt)
    await audit(db, admin.id, "questions.new_version", "question", question_id, {"version_no": v.version_no})
    await db.commit()
    return question_version_out(q, v, True)


@router.post("/admin/questions/{question_id}/review")
async def review(question_id: str, approve: bool, note: str | None = None,
                 admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    got = await _get_question_version(db, question_id)
    if got is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "题目不存在。")
    q, v = got
    if approve:
        v.review_state = "approved"
        q.status = "published"
    else:
        v.review_state = "rejected"
        q.status = "draft"
    await audit(db, admin.id, "questions.review", "question", question_id,
                {"approve": approve, "note": note, "version_no": v.version_no})
    await db.commit()
    return question_version_out(q, v, True)


@router.post("/admin/questions/{question_id}/retire")
async def retire(question_id: str, admin: User = Depends(require_admin),
                 db: AsyncSession = Depends(get_db)) -> dict:
    q = await db.get(Question, question_id)
    if q is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "题目不存在。")
    q.status = "retired"  # 停用不影响历史赛果（§13.3）
    await audit(db, admin.id, "questions.retire", "question", question_id, {})
    await db.commit()
    return {"ok": True}


class ImportIn(BaseModel):
    pool: str = POOL_OFFICIAL
    csv_text: str = Field(min_length=1, max_length=2_000_000)


@router.post("/admin/questions/import")
async def import_csv(body: ImportIn, admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_db)) -> dict:
    """CSV 列：prompt,optionA,optionB,optionC,optionD,correct(A-D),explanation,subject,difficulty"""
    if body.pool not in (POOL_OFFICIAL, POOL_PRACTICE):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "pool 必须是 official/practice。")
    reader = csv.DictReader(io.StringIO(body.csv_text))
    need = {"prompt", "optionA", "optionB", "optionC", "optionD", "correct", "subject"}
    if reader.fieldnames is None or not need <= set(h.strip() for h in reader.fieldnames):
        raise AppError(422, ErrorCode.VALIDATION_ERROR, f"CSV 缺少必需列：{sorted(need)}")
    created, duplicates, errors = 0, 0, []
    existing_hashes = set((await db.execute(
        select(Question.prompt_hash).where(Question.pool == body.pool)
    )).scalars())
    for i, row in enumerate(reader, start=2):
        try:
            prompt = (row.get("prompt") or "").strip()
            opts = [(row.get(f"option{L}") or "").strip() for L in LETTERS[:4]]
            correct = (row.get("correct") or "A").strip().upper()[:1]
            subject = (row.get("subject") or "常识").strip()
            if not prompt or any(not o for o in opts):
                raise ValueError("题干或选项为空")
            if correct not in "ABCD":
                raise ValueError("correct 必须是 A-D")
            h = normalize_prompt(prompt)
            if h in existing_hashes:
                duplicates += 1
                continue
            ids = _option_ids(4)
            qid = new_id()
            diff_raw = (row.get("difficulty") or "1").strip()
            q = Question(id=qid, subject=subject, difficulty=int(diff_raw) if diff_raw.isdigit() else 1,
                         language=(row.get("language") or "zh").strip() or "zh",
                         pool=body.pool, status="draft", prompt_hash=h, created_by=admin.id)
            v = QuestionVersion(id=new_id(), question_id=qid, version_no=1, prompt=prompt,
                                options_json=[{"option_id": ids[j], "text": opts[j]} for j in range(4)],
                                correct_option_id=ids["ABCD".index(correct)],
                                explanation=(row.get("explanation") or "").strip() or None,
                                source=(row.get("source") or "").strip() or None,
                                review_state="pending", created_by=admin.id)
            db.add_all([q, v])
            q.current_version_id = v.id
            existing_hashes.add(h)
            created += 1
        except Exception as exc:  # 单行错误不阻断整批
            errors.append({"line": i, "error": str(exc)})
    await audit(db, admin.id, "questions.import", "pool", body.pool,
                {"created": created, "duplicates": duplicates, "errors": len(errors)})
    await db.commit()
    return {"created": created, "duplicates": duplicates, "errors": errors[:20], "error_total": len(errors)}


@router.get("/admin/questions/export")
async def export_csv(pool: str = POOL_OFFICIAL, admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(
        select(Question, QuestionVersion).join(QuestionVersion, QuestionVersion.id == Question.current_version_id)
        .where(Question.pool == pool)
    )).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["prompt", "optionA", "optionB", "optionC", "optionD", "correct",
                "explanation", "subject", "difficulty"])
    for q, v in rows:
        texts = {o["option_id"]: o["text"] for o in v.options_json}
        correct_letter = LETTERS[int(v.correct_option_id[1:]) - 1]
        w.writerow([v.prompt, texts.get("o1", ""), texts.get("o2", ""), texts.get("o3", ""),
                    texts.get("o4", ""), correct_letter, v.explanation or "", q.subject, q.difficulty])
    return {"csv_text": buf.getvalue(), "count": len(rows)}


# ── 控制台：批量与单题操作 ──────────────────────────────────────────────────
# 路由声明顺序提醒（FastAPI 按声明先后匹配）：
#   · `/admin/questions/{question_id}` 这种"吞参数"的路径必须排在 `stats`、
#     `export` 这些固定字面量之后，否则 /admin/questions/stats 会被当成一道题的 id
#     （404 题目不存在）——所以单题详情写在所有固定字面量 GET 之后。
#   · POST 的 bulk-* 与 /{question_id}/restore 段数与字面量都不同，互不遮蔽。


class BulkReviewIn(BaseModel):
    """批量审核入参。ids 上限与列表一页上限一致：一次点选的最大范围就是"看全的一屏"。"""
    ids: list[str] = Field(min_length=1, max_length=MAX_BULK_IDS)
    approve: bool


class BulkSubjectIn(BaseModel):
    """批量改学科入参。subject 自己校验（先 strip 再判长度），此处不限长度。"""
    ids: list[str] = Field(min_length=1, max_length=MAX_BULK_IDS)
    subject: str


class BulkRetireIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=MAX_BULK_IDS)


@router.post("/admin/questions/bulk-review")
async def bulk_review(body: BulkReviewIn, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """批量审核（通过 / 退回）。

    为什么跳过而不是整批失败：控制台列表是几秒前拉的，ids 里混进刚被删掉或刚停用的题
    属于正常竞态，为一两个失效 id 让整批 200 道白点一遍比跳过更糟。
    已停用的题一律不参与审核：它的状态由 retired 说了算，
    批准它等于悄悄把一道已停用的题放回抽题池。
    """
    loaded = await _load_current_versions(db, body.ids)
    updated, skipped = 0, []
    for qid in _unique_ids(body.ids):
        got = loaded.get(qid)
        if got is None:
            # 含"题在但当前版本缺失"的脏数据：审不了，按找不到报回
            skipped.append({"id": qid, "why": "not_found"})
            continue
        question, version = got
        if question.status == "retired":
            skipped.append({"id": qid, "why": "retired"})
            continue
        if body.approve:
            version.review_state = "approved"
            question.status = "published"
        else:
            version.review_state = "rejected"
            question.status = "draft"
        updated += 1
    # 一条审计覆盖整批：ids 逐个写审计会把审计表刷满，反而看不出"谁在什么时候批了一批"
    await audit(db, admin.id, "questions.bulk_review", "question", None,
                {"approve": body.approve, "updated": updated})
    await db.commit()
    return {"updated": updated, "skipped": skipped}


@router.post("/admin/questions/bulk-subject")
async def bulk_subject(body: BulkSubjectIn, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)) -> dict:
    """批量改学科（控制台里最常见的整理动作：导入后按学科归类）。

    只改 Question.subject，不动任何版本：学科是题目的分类属性，改它不需要重新审核，
    否则整理一次学科就要把上百道题重新推回待审队列。
    """
    subject = (body.subject or "").strip()
    if not 1 <= len(subject) <= 32:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "学科名称长度需为 1-32 个字符。")
    loaded = await _load_questions(db, body.ids)
    updated, skipped = 0, []
    for qid in _unique_ids(body.ids):
        question = loaded.get(qid)
        if question is None:
            skipped.append({"id": qid, "why": "not_found"})
            continue
        if question.status == "retired":
            # 停用的题不再参与任何题库整理：改它只会让历史归档更难追溯
            skipped.append({"id": qid, "why": "retired"})
            continue
        question.subject = subject
        updated += 1
    await audit(db, admin.id, "questions.bulk_subject", "question", None,
                {"subject": subject, "updated": updated})
    await db.commit()
    return {"updated": updated, "skipped": skipped}


@router.post("/admin/questions/bulk-retire")
async def bulk_retire(body: BulkRetireIn, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """批量停用（不是删除）。

    停用只把题移出抽题池，版本与历史赛果全部保留（§13.3）；
    已经停用的题按 skipped 报回，前端才能提示"其中 N 道本来就没启用"。
    """
    loaded = await _load_questions(db, body.ids)
    updated, skipped = 0, []
    for qid in _unique_ids(body.ids):
        question = loaded.get(qid)
        if question is None:
            skipped.append({"id": qid, "why": "not_found"})
            continue
        if question.status == "retired":
            skipped.append({"id": qid, "why": "retired"})
            continue
        question.status = "retired"
        updated += 1
    await audit(db, admin.id, "questions.bulk_retire", "question", None, {"updated": updated})
    await db.commit()
    return {"updated": updated, "skipped": skipped}


@router.get("/admin/questions/{question_id}")
async def question_detail(question_id: str, _admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    """单题详情（控制台右侧抽屉）。

    为什么单独一个详情接口：列表为了跑得快只给当前版本，而控制台要看
    "这道题改过几版、每版审到哪一步"，否则编辑页面会覆盖掉未审完的改动。
    """
    got = await _get_question_version(db, question_id)
    if got is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "题目不存在。")
    q, v = got
    versions = (await db.execute(
        select(QuestionVersion).where(QuestionVersion.question_id == question_id)
        .order_by(QuestionVersion.version_no.desc())
    )).scalars().all()
    used = await _used_in_matches_map(db, [question_id])
    out = admin_question_out(q, v, used.get(question_id, 0))
    out["history"] = [{"version_no": item.version_no, "review_state": item.review_state,
                       "prompt": item.prompt, "created_at": iso_z(item.created_at)}
                      for item in versions]
    return out


@router.post("/admin/questions/{question_id}/restore")
async def restore(question_id: str, admin: User = Depends(require_admin),
                  db: AsyncSession = Depends(get_db)) -> dict:
    """恢复停用（`retire` 的逆操作）。

    恢复到哪个状态由**当前版本**决定，而不是无脑回到 published：
      · 当前版本已审核通过 → published，可以重新被抽题；
      · 当前版本 pending/rejected → draft，回到待审；
    否则一道没审过的题会借"恢复"绕过审核直接进正式比赛题库。
    对本来就没停用的题，这个规则算出来就是它现有状态，因此是幂等空操作而非报错。
    """
    got = await _get_question_version(db, question_id)
    if got is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "题目不存在。")
    q, v = got
    q.status = "published" if v.review_state == "approved" else "draft"
    await audit(db, admin.id, "questions.restore", "question", question_id,
                {"status": q.status, "version_no": v.version_no})
    await db.commit()
    return question_version_out(q, v, True)


@router.delete("/admin/questions/{question_id}")
async def delete_question(question_id: str, admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    """物理删除（只允许删从未被比赛用过的题）。

    为什么被用过的题一律拒绝而不是级联删：比赛回合、选手作答与成绩单都按
    match_question / 版本快照落库，删掉题目会让历史赛果出现"无题可查"的空洞，
    赛后无法复盘也无法受理申诉。所以这里 409 并引导管理员改用「停用」（§13.3）。
    """
    question = await db.get(Question, question_id)
    if question is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "题目不存在。")
    used = (await db.execute(
        select(func.count()).select_from(MatchQuestion)
        .where(MatchQuestion.question_id == question_id)
    )).scalar_one()
    if used:
        raise AppError(409, ErrorCode.VALIDATION_ERROR,
                       "该题已被比赛使用，不能删除；请改为「停用」以保留历史成绩。")
    version_ids = (await db.execute(
        select(QuestionVersion.id).where(QuestionVersion.question_id == question_id)
    )).scalars().all()
    # 先删版本再删题：question_versions.question_id 是指向 questions 的外键
    await db.execute(delete(QuestionVersion).where(QuestionVersion.question_id == question_id))
    await db.delete(question)
    await audit(db, admin.id, "questions.delete", "question", question_id,
                {"deleted_versions": len(version_ids)})
    await db.commit()
    return {"ok": True, "deleted_versions": len(version_ids)}
