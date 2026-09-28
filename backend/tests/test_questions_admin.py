"""题库管理端（控制台"一屏看全题库"）接口验收。

覆盖三件事：
  1. **看得全**：列表的搜索/过滤/排序、每题的引用次数、单题详情与历史版本、
     首屏大盘（stats）的分面计数；
  2. **改得动**：批量审核 / 批量改学科 / 批量停用 / 恢复 / 删除；
  3. **改不坏**：被比赛用过的题不许删、恢复不能绕过审核（"恢复"必须看当前版本的审核态）、
     每一次变更都要在审计里留痕。

全部通过真实 HTTP 调用（client fixture）验收，不直接调 router 函数——
路由遮蔽（例如 /admin/questions/stats 被 /{question_id} 吞掉）只有走 ASGI 才看得见。

统计类断言一律用"与列表口径交叉验证 + 下界"而不是写死数字：
测试库是 session 级共享的，其它测试文件也会往题库里塞题（练习池尤其多）。
"""
import uuid

from tests.conftest import login, make_match

ADMIN_USER, ADMIN_PASSWORD = "admin", "admin-pass-123"


async def _admin(client) -> None:
    await login(client, ADMIN_USER, ADMIN_PASSWORD)


async def _new_question(client, **over) -> dict:
    """建一道题并返回创建响应（默认练习池、待审 —— 不干扰正式池的抽题）。"""
    body = {
        "subject": "科学常识",
        "difficulty": 1,
        "language": "zh",
        "pool": "practice",
        "prompt": f"控制台测试题 {uuid.uuid4().hex[:8]}？",
        "options": [{"text": "对"}, {"text": "错"}],
        "correct_index": 0,
        "explanation": "测试解析",
        "source": "测试来源",
    }
    body.update(over)
    r = await client.post("/api/v1/admin/questions", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _detail(client, question_id: str) -> dict:
    r = await client.get(f"/api/v1/admin/questions/{question_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def _all_questions(client, **params) -> list[dict]:
    r = await client.get("/api/v1/admin/questions", params={"size": 200, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ── 1. 首屏大盘 ─────────────────────────────────────────────────────────────

async def test_stats_shape_and_seeded_counts(client, db_setup):
    """stats：两个池、五个计数键齐全无 null，且与列表口径互相印证。"""
    await _admin(client)
    r = await client.get("/api/v1/admin/questions/stats")
    assert r.status_code == 200, r.text
    stats = r.json()
    assert set(stats) == {"pools", "subjects", "review_queue", "duplicates"}
    assert set(stats["pools"]) == {"official", "practice"}
    for bucket in stats["pools"].values():
        assert set(bucket) == {"total", "draft", "published", "retired", "pending_review"}
        assert all(isinstance(v, int) for v in bucket.values()), bucket
    assert isinstance(stats["review_queue"], int) and isinstance(stats["duplicates"], int)

    official = stats["pools"]["official"]
    assert official["total"] >= 6, stats
    assert official["published"] >= 6, stats
    # 三种状态之和必须等于总数（否则分面与总数会互相打架）
    assert official["draft"] + official["published"] + official["retired"] == official["total"]

    # 种子里的 6 道 official/科学常识 题必须出现在学科分面里
    seeded = next(s for s in stats["subjects"] if s["subject"] == "科学常识")
    assert seeded["official"] >= 6, stats["subjects"]
    assert seeded["published"] >= 6
    assert seeded["total"] == seeded["draft"] + seeded["published"] + seeded["retired"]
    assert seeded["total"] == seeded["official"] + seeded["practice"]

    # 学科分面按 total 降序、同 total 按学科名升序
    ordered = sorted(stats["subjects"], key=lambda s: (-s["total"], s["subject"]))
    assert stats["subjects"] == ordered, stats["subjects"]

    # 各学科分池计数之和 == 池总数（交叉验证：分面不是各算各的）
    assert sum(s["official"] for s in stats["subjects"]) == official["total"]
    assert sum(s["practice"] for s in stats["subjects"]) == stats["pools"]["practice"]["total"]

    # review_queue 的定义 = 全部池中当前版本待审的题数，用列表的 total 交叉验证
    r = await client.get("/api/v1/admin/questions",
                         params={"review_state": "pending", "size": 1})
    assert r.json()["total"] == stats["review_queue"]

    # duplicates = 同池内 prompt_hash 重复的"多出来的"题数。
    # 用"增量"验证而不是按题干文本重算全库：测试库是 session 级共享的，
    # 别的用例会灌入大量**题干相同但 prompt_hash 不同**的练习题
    # （例如 test_practice_leaderboard 的 "练习题 0/1/2？"），按文本重算会得出与定义不符的数字。
    dup_prompt = f"重复题干 {uuid.uuid4().hex[:8]}？"
    await _new_question(client, prompt=dup_prompt)
    once = (await client.get("/api/v1/admin/questions/stats")).json()
    assert once["duplicates"] == stats["duplicates"], "同题干只有一道时不算重复"
    await _new_question(client, prompt=dup_prompt)
    twice = (await client.get("/api/v1/admin/questions/stats")).json()
    assert twice["duplicates"] == stats["duplicates"] + 1, twice
    # 同题干但换了池：不算重复（重复只在池内判定）
    await _new_question(client, prompt=dup_prompt, pool="official")
    other_pool = (await client.get("/api/v1/admin/questions/stats")).json()
    assert other_pool["duplicates"] == stats["duplicates"] + 1, other_pool


# ── 2. 搜索 / 过滤 / 排序 ───────────────────────────────────────────────────

async def test_list_search_and_filters(client, db_setup):
    """q 命中题干/学科/来源/解析四列；subject/difficulty/review_state 过滤生效；排序确定。"""
    await _admin(client)
    marker = f"Mk{uuid.uuid4().hex[:8]}"
    in_prompt = await _new_question(client, prompt=f"{marker} 出现在题干？")
    in_subject = await _new_question(client, subject=f"科目{marker}")
    in_source = await _new_question(client, source=f"来源{marker}")
    in_explanation = await _new_question(client, explanation=f"解析{marker}")
    wanted = {in_prompt["question_id"], in_subject["question_id"],
              in_source["question_id"], in_explanation["question_id"]}

    for needle in (marker, marker.upper()):  # 大小写不敏感
        r = await client.get("/api/v1/admin/questions", params={"q": needle, "size": 200})
        assert r.status_code == 200, r.text
        found = {i["question_id"] for i in r.json()["items"]}
        assert found == wanted, f"q={needle} 命中 {found}，期望 {wanted}"

    # subject 精确过滤
    r = await client.get("/api/v1/admin/questions",
                         params={"subject": f"科目{marker}", "size": 200})
    assert [i["question_id"] for i in r.json()["items"]] == [in_subject["question_id"]]
    assert r.json()["total"] == 1

    # difficulty 过滤
    hard = await _new_question(client, difficulty=3)
    r = await client.get("/api/v1/admin/questions", params={"difficulty": 3, "size": 200})
    items = r.json()["items"]
    assert hard["question_id"] in {i["question_id"] for i in items}
    assert all(i["difficulty"] == 3 for i in items)
    r = await client.get("/api/v1/admin/questions", params={"difficulty": 9})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"

    # review_state 过滤（新建默认 pending / approve=True 直接 approved）
    approved = await _new_question(client, approve=True)
    r = await client.get("/api/v1/admin/questions",
                         params={"review_state": "pending", "size": 200})
    pend = r.json()["items"]
    assert in_prompt["question_id"] in {i["question_id"] for i in pend}
    assert approved["question_id"] not in {i["question_id"] for i in pend}
    assert all(i["review_state"] == "pending" for i in pend)
    r = await client.get("/api/v1/admin/questions",
                         params={"review_state": "approved", "size": 200})
    appr = r.json()["items"]
    assert approved["question_id"] in {i["question_id"] for i in appr}
    assert all(i["review_state"] == "approved" for i in appr)
    r = await client.get("/api/v1/admin/questions", params={"review_state": "乱填"})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"

    # sort=subject：两次调用顺序完全一致，且学科名非降序
    first = await _all_questions(client, sort="subject")
    second = await _all_questions(client, sort="subject")
    assert [i["question_id"] for i in first] == [i["question_id"] for i in second]
    subjects = [i["subject"] for i in first]
    assert subjects == sorted(subjects), subjects
    # 也覆盖另外三种排序：必须都能跑通且不改变总数
    for mode in ("created_desc", "created_asc", "difficulty"):
        assert len(await _all_questions(client, sort=mode)) == len(first)
    r = await client.get("/api/v1/admin/questions", params={"sort": "drop table"})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"

    # size 上限收敛到 200（超限不报错，直接夹住）
    r = await client.get("/api/v1/admin/questions", params={"size": 5000})
    assert r.json()["size"] == 200 and len(r.json()["items"]) <= 200


# ── 3. 单题详情：历史版本 + 引用次数 ────────────────────────────────────────

async def test_detail_history_and_used_in_matches(client, db_setup):
    """详情带 history（version_no 1）与 used_in_matches；被比赛引用后 ≥ 1。"""
    await _admin(client)
    created = await _new_question(client, approve=True)
    qid = created["question_id"]

    detail = await _detail(client, qid)
    assert detail["question_id"] == qid
    assert detail["used_in_matches"] == 0
    assert detail["correct_index"] == 0
    assert detail["created_at"] and detail["updated_at"]
    assert detail["explanation"] == "测试解析" and detail["source"] == "测试来源"
    assert [h["version_no"] for h in detail["history"]] == [1]
    assert detail["history"][0]["review_state"] == "approved"
    assert detail["history"][0]["prompt"] == detail["prompt"]
    assert detail["history"][0]["created_at"]

    # 建一场比赛把种子题冻进 match_questions → 该题引用次数 ≥ 1
    await make_match(db_setup, client, question_count=1, with_roster=False)
    used_id = db_setup["question_ids"][0]
    assert (await _detail(client, used_id))["used_in_matches"] >= 1
    # 未被引用的自建题仍是 0
    assert (await _detail(client, qid))["used_in_matches"] == 0

    # 不存在的 id：404 而不是被 stats/export 遮蔽
    r = await client.get("/api/v1/admin/questions/does-not-exist")
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "NOT_FOUND" and r.json()["message"] == "题目不存在。"
    # 字面量子路径没有被 {question_id} 吞掉
    assert (await client.get("/api/v1/admin/questions/stats")).status_code == 200
    assert (await client.get("/api/v1/admin/questions/export")).status_code == 200


# ── 4 / 5. 录题与改题 ───────────────────────────────────────────────────────

async def test_create_approve_flag(client, db_setup):
    """approve=True 直接 published/approved；默认仍是 draft/pending（既有行为不变）。"""
    await _admin(client)
    published = await _new_question(client, approve=True)
    assert published["status"] == "published"
    assert published["review_state"] == "approved"

    draft = await _new_question(client)
    assert draft["status"] == "draft"
    assert draft["review_state"] == "pending"
    assert (await _detail(client, draft["question_id"]))["status"] == "draft"


async def test_put_creates_version_two(client, db_setup):
    """改题 = 新版本：version_no 变 2、历史 2 条、状态回到 draft/待审。"""
    await _admin(client)
    created = await _new_question(client, approve=True)
    qid = created["question_id"]
    assert created["version_no"] == 1 and created["status"] == "published"

    body = {
        "subject": "科学常识", "difficulty": 2, "language": "zh", "pool": "practice",
        "prompt": "改过的题干 ？", "options": [{"text": "对"}, {"text": "错"}],
        "correct_index": 1, "explanation": "新解析", "source": "新来源",
        "approve": True,  # PUT 上不生效：新版本必须重新审核，不能顺手自我批准
    }
    r = await client.put(f"/api/v1/admin/questions/{qid}", json=body)
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["version_no"] == 2
    assert updated["review_state"] == "pending"
    assert updated["status"] == "draft"

    detail = await _detail(client, qid)
    assert detail["version_no"] == 2
    assert [h["version_no"] for h in detail["history"]] == [2, 1]  # 新版在前
    assert detail["correct_index"] == 1
    assert (await _all_questions(client, q="改过的题干"))[0]["question_id"] == qid


# ── 6 / 7 / 8. 批量操作 ─────────────────────────────────────────────────────

async def test_bulk_review_approve_and_skips(client, db_setup):
    """批量通过两道草稿；不存在的 id → not_found；已停用的题 → retired（不整批失败）。"""
    await _admin(client)
    a = await _new_question(client)
    b = await _new_question(client)
    retired = await _new_question(client)
    r = await client.post(f"/api/v1/admin/questions/{retired['question_id']}/retire")
    assert r.status_code == 200, r.text

    bogus = f"missing-{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/v1/admin/questions/bulk-review",
                          json={"ids": [a["question_id"], b["question_id"],
                                        bogus, retired["question_id"]], "approve": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 2, body
    assert body["skipped"] == [{"id": bogus, "why": "not_found"},
                               {"id": retired["question_id"], "why": "retired"}], body

    for qid in (a["question_id"], b["question_id"]):
        detail = await _detail(client, qid)
        assert detail["status"] == "published" and detail["review_state"] == "approved"
    # 被跳过的停用题状态没被动过
    assert (await _detail(client, retired["question_id"]))["status"] == "retired"

    # 反向：批量退回 → draft / rejected
    r = await client.post("/api/v1/admin/questions/bulk-review",
                          json={"ids": [a["question_id"]], "approve": False})
    assert r.json()["updated"] == 1
    rejected = await _detail(client, a["question_id"])
    assert rejected["status"] == "draft" and rejected["review_state"] == "rejected"

    # 非法入参：空 ids 被拒
    r = await client.post("/api/v1/admin/questions/bulk-review", json={"ids": [], "approve": True})
    assert r.status_code == 422, r.text
    # 超过 200 个 id 被拒
    r = await client.post("/api/v1/admin/questions/bulk-review",
                          json={"ids": [f"x{i}" for i in range(201)], "approve": True})
    assert r.status_code == 422, r.text


async def test_bulk_subject_updates_stats(client, db_setup):
    """批量改学科：两道题的学科被改掉，stats.subjects 立刻反映新学科。"""
    await _admin(client)
    old_subject = f"旧科目{uuid.uuid4().hex[:6]}"
    new_subject = f"新科目{uuid.uuid4().hex[:6]}"
    a = await _new_question(client, subject=old_subject)
    b = await _new_question(client, subject=old_subject)
    bogus = f"missing-{uuid.uuid4().hex[:8]}"

    r = await client.post("/api/v1/admin/questions/bulk-subject",
                          json={"ids": [a["question_id"], b["question_id"], bogus],
                                "subject": f"  {new_subject}  "})  # 前后空格应被 strip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 2, body
    assert body["skipped"] == [{"id": bogus, "why": "not_found"}], body

    stats = (await client.get("/api/v1/admin/questions/stats")).json()
    facet = next(s for s in stats["subjects"] if s["subject"] == new_subject)
    assert facet["total"] == 2 and facet["practice"] == 2
    assert all(s["subject"] != old_subject for s in stats["subjects"]), stats["subjects"]
    assert {i["question_id"] for i in await _all_questions(client, subject=new_subject)} == \
        {a["question_id"], b["question_id"]}

    # 学科名太短/太长被拒（strip 之后再判长度）
    r = await client.post("/api/v1/admin/questions/bulk-subject",
                          json={"ids": [a["question_id"]], "subject": "   "})
    assert r.status_code == 422, r.text
    r = await client.post("/api/v1/admin/questions/bulk-subject",
                          json={"ids": [a["question_id"]], "subject": "长" * 33})
    assert r.status_code == 422, r.text


async def test_bulk_retire_then_restore(client, db_setup):
    """批量停用 → 恢复：当前版本已通过的题恢复成 published；待审的题只能回 draft。"""
    await _admin(client)
    approved = await _new_question(client, approve=True)
    pending = await _new_question(client)

    r = await client.post("/api/v1/admin/questions/bulk-retire",
                          json={"ids": [approved["question_id"], pending["question_id"]]})
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": 2, "skipped": []}, r.json()
    for qid in (approved["question_id"], pending["question_id"]):
        assert (await _detail(client, qid))["status"] == "retired"

    # 重复停用：算 skipped 而不是报错
    r = await client.post("/api/v1/admin/questions/bulk-retire",
                          json={"ids": [approved["question_id"]]})
    assert r.json() == {"updated": 0,
                        "skipped": [{"id": approved["question_id"], "why": "retired"}]}, r.json()

    # 恢复：已审通过的版本可以重新进池
    r = await client.post(f"/api/v1/admin/questions/{approved['question_id']}/restore")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published" and r.json()["review_state"] == "approved"
    # 恢复：未审的版本只能回到草稿，不能借恢复绕过审核
    r = await client.post(f"/api/v1/admin/questions/{pending['question_id']}/restore")
    assert r.json()["status"] == "draft" and r.json()["review_state"] == "pending"

    # 对未停用的题是幂等空操作
    r = await client.post(f"/api/v1/admin/questions/{approved['question_id']}/restore")
    assert r.status_code == 200 and r.json()["status"] == "published"
    r = await client.post("/api/v1/admin/questions/does-not-exist/restore")
    assert r.status_code == 404 and r.json()["message"] == "题目不存在。"


# ── 9. 删除的两条路 ─────────────────────────────────────────────────────────

async def test_delete_refuses_used_question(client, db_setup):
    """被比赛引用过的题：409 + 明确的拒绝文案，且题目仍在。"""
    await _admin(client)
    await make_match(db_setup, client, question_count=1, with_roster=False)
    used_id = db_setup["question_ids"][0]
    detail = await _detail(client, used_id)
    assert detail["used_in_matches"] >= 1

    r = await client.delete(f"/api/v1/admin/questions/{used_id}")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["message"] == "该题已被比赛使用，不能删除；请改为「停用」以保留历史成绩。"
    assert (await client.get(f"/api/v1/admin/questions/{used_id}")).status_code == 200
    # 被拒之后题目状态原样保留（这里刻意不去停用种子题，避免污染同一 session 库里的其它用例）
    assert (await _detail(client, used_id))["status"] == "published"


async def test_delete_unused_question(client, db_setup):
    """未被使用的题：删除成功，版本一并删除，随后详情 404。"""
    await _admin(client)
    created = await _new_question(client)
    qid = created["question_id"]
    # 再改一版，验证 deleted_versions 统计的是**全部**历史版本
    r = await client.put(f"/api/v1/admin/questions/{qid}", json={
        "subject": "科学常识", "difficulty": 1, "language": "zh", "pool": "practice",
        "prompt": "第二版题干 ？", "options": [{"text": "对"}, {"text": "错"}],
        "correct_index": 0,
    })
    assert r.status_code == 200 and r.json()["version_no"] == 2

    r = await client.delete(f"/api/v1/admin/questions/{qid}")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "deleted_versions": 2}, r.json()
    assert (await client.get(f"/api/v1/admin/questions/{qid}")).status_code == 404
    assert all(i["question_id"] != qid for i in await _all_questions(client))
    r = await client.delete(f"/api/v1/admin/questions/{qid}")
    assert r.status_code == 404 and r.json()["message"] == "题目不存在。"


# ── 10. 所有变更都留痕 ──────────────────────────────────────────────────────

async def test_mutations_are_audited(client, db_setup):
    """批量/单题变更都要在管理端审计里查得到（含 detail 里的数量与方向）。"""
    await _admin(client)
    a = await _new_question(client)
    b = await _new_question(client)
    await client.post("/api/v1/admin/questions/bulk-review",
                      json={"ids": [a["question_id"], b["question_id"]], "approve": True})
    await client.post("/api/v1/admin/questions/bulk-subject",
                      json={"ids": [a["question_id"]], "subject": "审计科目"})
    await client.post("/api/v1/admin/questions/bulk-retire", json={"ids": [a["question_id"]]})
    await client.post(f"/api/v1/admin/questions/{a['question_id']}/restore")
    deleted = await _new_question(client)
    await client.delete(f"/api/v1/admin/questions/{deleted['question_id']}")

    r = await client.get("/api/v1/admin/audit", params={"prefix": "questions.", "limit": 200})
    assert r.status_code == 200, r.text
    logs = r.json()["items"]
    actions = {log["action"] for log in logs}
    for expected in ("questions.create", "questions.bulk_review", "questions.bulk_subject",
                     "questions.bulk_retire", "questions.restore", "questions.delete"):
        assert expected in actions, f"缺少审计动作 {expected}：{sorted(actions)}"

    bulk = next(log for log in logs if log["action"] == "questions.bulk_review")
    assert bulk["detail"]["approve"] is True
    assert bulk["detail"]["updated"] == 2
    restore = next(log for log in logs if log["action"] == "questions.restore")
    assert restore["entity_id"] == a["question_id"]
    assert restore["detail"]["status"] == "published"
    delete_log = next(log for log in logs if log["action"] == "questions.delete")
    assert delete_log["detail"]["deleted_versions"] == 1
