"""规则纯函数测试：精确分数、平分、门槛（§15.3、§10.3）+ 四种评分标准。"""
from app.modules.competitions.rules import (
    Fraction, HouseStanding, house_score, is_official_result,
    min_valid_questions, normalize_best_to_100, participant_score, rank_standings,
    score_by_method, scoring_note,
)


def test_fraction_exact_compare_beats_float_display():
    # 显示值同为 0.67，但精确值不同：2/3 ≈ 0.6667 > 6666/10000 = 0.6666
    a = Fraction(2, 3)
    b = Fraction(6666, 10000)
    assert a.display() == 0.67
    assert b.display() == 0.67
    assert a.cmp(b) > 0, "显示相同的分数必须按精确值分出高下"
    assert b.cmp(a) < 0
    assert Fraction(3, 9).cmp(Fraction(1, 3)) == 0


def test_house_score_is_participant_average():
    # 20 题示例（需求 §15.3 表格）：白虎 10 人 140 对 → 70 分
    f = house_score(correct_total=140, participants=10, valid_questions=20)
    assert f.display() == 70.0
    # 等价于成员个人分平均：每人 14/20 → 70
    individuals = [participant_score(14, 20) for _ in range(10)]
    avg = sum(x.value() for x in individuals) / len(individuals)
    assert abs(f.value() - avg) < 1e-9


def test_ranking_with_tie_1224():
    s = [
        HouseStanding("h1", "a", "A", 10, 140, house_score(140, 10, 20), True),
        HouseStanding("h2", "b", "B", 20, 300, house_score(300, 20, 20), True),
        HouseStanding("h3", "c", "C", 12, 168, house_score(168, 12, 20), True),
        HouseStanding("h4", "d", "D", 8, 128, house_score(128, 8, 20), True),
    ]
    ranked = rank_standings(s)
    by_key = {x.house_key: x for x in ranked}
    assert by_key["d"].rank == 1 and not by_key["d"].is_tie      # 80 分第一
    assert by_key["b"].rank == 2 and not by_key["b"].is_tie      # 75 分第二
    assert by_key["a"].rank == 3 and by_key["a"].is_tie          # 70 分并列第三
    assert by_key["c"].rank == 3 and by_key["c"].is_tie


def test_min_valid_questions_ceil():
    assert min_valid_questions(20) == 16
    assert min_valid_questions(10, 0.8) == 8
    assert min_valid_questions(7) == 6  # 5.6 向上取整


def test_official_conditions():
    ok, reason = is_official_result(16, 16, [True, True, True, True])
    assert ok and reason is None
    ok, reason = is_official_result(15, 16, [True] * 4)
    assert not ok and "低于赛前门槛" in reason
    ok, reason = is_official_result(16, 16, [True, False, True, True])
    assert not ok and "人数" in reason
    ok, reason = is_official_result(0, 16, [True] * 4)
    assert not ok and "无有效计分回合" in reason


# ═══ 四种评分标准（后台建赛时选择，四院共用同一套）═══════════════════════════

def test_scoring_avg_matches_legacy_house_score():
    """avg 必须与旧公式**逐位相等**，否则历史成绩与新成绩不可比。"""
    members = [14, 16, 12, 18, 10]      # 5 人，20 题
    a = score_by_method("avg", members, 20)
    b = house_score(sum(members), len(members), 20)
    assert a.cmp(b) == 0, f"avg 与旧公式不一致：{a} vs {b}"
    assert a.display() == 70.0


def test_scoring_total_normalized_to_best_100():
    """总分制必须先取原始总分、再由 calculate_result 归一化到榜首 100。

    原始总分与人数同量纲：3 人全对 54 对、10 人各 10 对共 100 对。
    归一化后榜首（10 人那院）= 100，另一院 = 54/100 × 100 = 54。
    若不做归一化，3 人那院会拿到 270 分（数值随人数漂移），
    既与 avg 的 0–100 不可比，也无法跨场比较 —— 这个错误是被本测试抓出来的。
    """
    raw_small = score_by_method("total", [18, 18, 18], 20)      # 原始 54
    raw_big = score_by_method("total", [10] * 10, 20)           # 原始 100
    assert raw_small.display() == 54.0
    assert raw_big.display() == 100.0
    small, big = normalize_best_to_100([raw_small, raw_big])
    assert big.display() == 100.0
    assert small.display() == 54.0, f"归一化错误：{small.display()}"


def test_normalize_best_to_100_edge_cases():
    """归一化的边界：全 0 不得除零；并列第一都应为 100。"""
    zeros = normalize_best_to_100([Fraction(0, 1), Fraction(0, 1)])
    assert [z.display() for z in zeros] == [0.0, 0.0]
    tie = normalize_best_to_100([Fraction(80, 1), Fraction(80, 1)])
    assert [t.display() for t in tie] == [100.0, 100.0]
    assert normalize_best_to_100([]) == []


def test_scoring_top_n_ignores_extra_members():
    """前 N 名平均：第 N 名之后的人不影响分数（抑制人海战术）。"""
    five = [20, 20, 20, 20, 20]
    with_extra = five + [0, 0, 0, 0, 0, 0, 0, 0]
    a = score_by_method("top_n", five, 20, top_n=5)
    b = score_by_method("top_n", with_extra, 20, top_n=5)
    assert a.cmp(b) == 0, "top_n 不应被第 N 名之后的人影响"
    assert a.display() == 100.0
    # N 大于实际人数时按实际人数取，不虚增分母
    c = score_by_method("top_n", [20, 10], 20, top_n=5)
    assert c.display() == 75.0


def test_scoring_accuracy_uses_answered_as_denominator():
    """正确率制：分母是**本院作答次数**，不作答不计入。"""
    # 4 人各答 20 题全对 → 100
    assert score_by_method("accuracy", [20, 20, 20, 20], 20,
                           answered_total=80).display() == 100.0
    # 4 人各答 10 题全对（其余未答）→ 仍 100，这正是该口径的特点
    assert score_by_method("accuracy", [10, 10, 10, 10], 20,
                           answered_total=40).display() == 100.0
    # answered_total 缺省时退化为 人数×有效题
    assert score_by_method("accuracy", [10, 10], 20).display() == 50.0


def test_scoring_all_methods_are_exact_fractions():
    """四种口径都必须返回精确分数，不能用浮点累积误差影响排名。"""
    for m in ("avg", "total", "top_n", "accuracy"):
        f = score_by_method(m, [7, 13, 11], 20, answered_total=55, top_n=2)
        assert isinstance(f, Fraction)
        assert f.den > 0
        # 精确性：分数值 = num/den，且用交叉相乘能精确比较自身
        assert f.cmp(Fraction(f.num, f.den)) == 0


def test_scoring_handles_empty_house():
    """空学院不得除零崩溃（人未签到时也会走到结算）。"""
    for m in ("avg", "total", "top_n", "accuracy"):
        f = score_by_method(m, [], 20)
        assert f.display() == 0.0


def test_scoring_note_covers_every_method():
    """每个口径都必须有对应的学生说明 —— 学生看到的每一分都要能追溯到算法。"""
    for m in ("avg", "total", "top_n", "accuracy"):
        note = scoring_note(m, 5)
        assert note and note != "学院得分按赛前公布的口径计算", f"{m} 缺少口径说明"
    assert "5" in scoring_note("top_n", 5)
