"""比赛规则纯函数（§4、§15.3）。

这里绝不能依赖前端动画、WebSocket 对象或请求上下文——只做可独立验证的数学与判定。
分数全程用整数分子/分母精确比较；显示时才转两位小数。
"""
from dataclasses import dataclass
from functools import cmp_to_key
from math import ceil, gcd


@dataclass(frozen=True)
class Fraction:
    num: int
    den: int

    def value(self) -> float:
        return self.num / self.den if self.den else 0.0

    def display(self) -> float:
        return round(self.value(), 2)

    def cmp(self, other: "Fraction") -> int:
        """精确比较：a/b vs c/d → 交叉相乘，绝不用浮点显示值裁决平分（§4）。"""
        left = self.num * other.den
        right = other.num * self.den
        return (left > right) - (left < right)

    def reduced(self) -> "Fraction":
        """约分到最简，并把整数符号收拢到分子。

        为什么必须约分：综合评分是把正确率与速度的分数相加，**分母相乘**会让分子分母
        迅速膨胀。而分数要落库到 `score_num INTEGER` —— PostgreSQL 的 Integer 是 int32，
        直接报 `value out of int32 range` 500；SQLite 是动态类型，会**默默存下**这个大数，
        于是同一个 bug 只在 PostgreSQL 上暴露（本项目实测踩到，只有真跑 PG 才发现）。
        """
        if self.den == 0:
            return Fraction(0, 1)
        n, d = self.num, self.den
        if d < 0:
            n, d = -n, -d
        g = gcd(abs(n), d) or 1
        return Fraction(n // g, d // g)

    def quantized(self, scale: int = 10_000) -> "Fraction":
        """把分数**精确四舍五入**到 1/scale 网格，返回最简分数。

        为什么需要它，而不只是约分：综合评分的分子是两个分数的加权和，
        约分之后仍可能出现「分子 9690177337 / 分母 96996900」这类形状 —— 两者都超出
        int32。而且**再约分也没用**（互质），这是数据类型与算法精度的根本冲突。

        解法是把"对外可见的精度"变成"存储的精度"：分数本来就是按两位小数展示的
        （`display()`），因此按 1/10000 网格量化既不损失任何对外可见信息，
        又把数值钉死在 int32 里（100.0000 → 1000000）。比较与排名依然精确，
        因为同一个网格上的两个分数可以直接比较。
        """
        if self.den == 0:
            return Fraction(0, 1)
        # round-half-up，用整数运算避免浮点误差：floor(n/d + 1/2)
        n, d = self.num, self.den
        if d < 0:
            n, d = -n, -d
        scaled = (2 * n * scale + d) // (2 * d)
        return Fraction(scaled, scale).reduced()


def participant_score(correct: int, valid_questions: int) -> Fraction:
    """个人分 = 100 × 正确题数 ÷ 有效题数（0–100）。错误/超时计零。"""
    return Fraction(correct * 100, max(valid_questions, 1))


def house_score(correct_total: int, participants: int, valid_questions: int) -> Fraction:
    """House 分 = 100 × 总答对 ÷ (冻结名单人数 × 有效题数) —— 等价于成员个人分算术平均（§15.3）。"""
    return Fraction(correct_total * 100, max(participants * valid_questions, 1))


# ═══ 评分标准（后台建赛时选择；四院共用同一套，否则学院之间不可比）═══════════

def score_by_method(method: str, member_correct: list[int], valid_questions: int,
                    answered_total: int | None = None,
                    top_n: int = 5) -> Fraction:
    """按选定的评分标准计算**一个学院**的官方分。

    member_correct：本院每个有效参赛者的答对题数（顺序无关）。
    answered_total：本院所有作答次数（仅 accuracy 用；缺省时按 人数×有效题 计）。

    四种口径的设计取舍（都返回 0–100 的精确分数，便于跨场比较）：
      avg       人均平均 —— 与旧实现一致。人多人少不影响量纲，但若各院上场人数不同，
                人少的一方每个人权重更大（这是"平均"的固有性质，不是缺陷）。
      total     总分累加 —— 人多占优。适合"鼓励全员上线"的场次，
                但各院人数不同时结果会向人多的一方倾斜，需在赛前说明。
      top_n     前 N 名平均 —— 抑制人海战术，适合不限人数赛；N 由赛前冻结。
      accuracy  正确率 —— 只看准度。**注意分母是本院的作答次数**：
                不作答不计入分母，因此"少答但答对"会得到高分——适合快速抢答赛，
                不适合"要求全员参与"的场次。
    """
    n = len(member_correct)
    valid = max(valid_questions, 1)

    if method == "total":
        # 总分制。注意：这里返回的是**原始总分**（答对总数），不乘 100。
        # 原因是它必须与各院人数同量纲才能比较，而归一化需要看到四院的全貌——
        # 归一化在 calculate_result 里统一做（榜首 = 100）。若在此处直接乘 100 除以题数，
        # 得分数值会随人数变化（3 人满分能到 270 分），与 avg 的 0–100 量纲不一致，
        # 跨场也无法比较（本项目测试抓到过这个设计错误）。
        return Fraction(sum(member_correct), 1)

    if method == "top_n":
        if n == 0:
            return Fraction(0, 1)
        k = max(1, min(top_n, n))
        top = sorted(member_correct, reverse=True)[:k]
        # 100 × 前 k 名答对总数 ÷ (k × 有效题数)
        return Fraction(sum(top) * 100, k * valid)

    if method == "accuracy":
        answered = answered_total if answered_total else n * valid
        return Fraction(sum(member_correct) * 100, max(answered, 1))

    # avg（默认）：100 × 总答对 ÷ (人数 × 有效题数)
    return Fraction(sum(member_correct) * 100, max(n * valid, 1))

def scoring_note(method: str, top_n: int = 5) -> str:
    """给学生的口径说明（结果页与首页展示用）。必须与上面的实现一一对应。"""
    return {
        "avg": "学院得分＝本院成员个人分的平均分",
        "total": "学院得分＝答对总数（答得最多的学院记为 100 分，其余按比例换算）",
        "top_n": f"学院得分＝本院个人分最高的 {top_n} 人的平均分",
        "accuracy": "学院得分＝本院作答的正确率（不作答不计入）",
    }.get(method, "学院得分按赛前公布的口径计算")


# ═══ 评分模式：只看正确率 / 综合评估（正确率 + 抢答速度，权重可调）═══════════
#
# 抢答赛制下"答对"与"答得快"都是能力，因此口径必须由主持人**赛前选**：
#   pure       只看正确率——最干净，答得慢但答得准不吃亏；
#   composite  综合评估——正确率与速度按权重合成，让"抢答"这件事真的值钱。
#
# 设计取舍（为什么速度用「相对本回合最快者」而不是绝对秒数）：
#   · 第 3 题花 9 秒、第 7 题花 4 秒，题目难度本来就不同；用绝对秒数会惩罚难题上的思考，
#     而那恰恰是知识竞赛想鼓励的；
#   · 相对口径下"这一题全场最快的人拿满分速度分"，只惩罚**同一题上的相对迟缓**，
#     与题目难度无关，四院之间也可比；
#   · 缺点：全员都很慢时，最慢的人分也不会太低（差距被压缩）。这是刻意的——
#     速度不该在大家都不确定的难题上决定名次。
#
# 只用**已答对的答卷**计速度分：答错本身已经在正确率项上受罚，
# 再用速度二次惩罚会让"抢答但答错"被罚两次，观感上也不合理。

def accuracy_of(correct: int, valid_questions: int) -> Fraction:
    """答对率（0–1 的精确分数）。"""
    return Fraction(correct, max(valid_questions, 1))


def round_speed_factor(elapsed_ms: int | None, fastest_ms: int | None) -> Fraction:
    """单份答卷的速度系数（0–1）：相对本回合最快正确者。

    没有用时（缺数据）按 0 计——宁可少给分，也不凭空给一个好看的数字。

    返回**已约分**：速度系数会被逐个相乘累加，不约分的话分母会连乘到几十亿，
    最终超出 PostgreSQL INTEGER（见 tests/test_buzz_scoring.py 的 int32 约束用例）。
    """
    if elapsed_ms is None or fastest_ms is None:
        return Fraction(0, 1)
    reference = max(int(fastest_ms), 1)
    if elapsed_ms <= 0:
        return Fraction(1, 1)
    return Fraction(reference, max(int(elapsed_ms), 1)).reduced()


def participation_rate(participated: int, expected: int) -> Fraction:
    """参与度（0–1）：实际参与量 ÷ 应参与量。

    为什么单独一个维度：知识竞赛的组织意图常常是"让尽量多的人真的上场"，
    但"人均平均"口径下**一个人都不交的学院并不吃亏**（分母只算上场的人）。
    把参与度做成可加权的一项，学校就能按场次意图在"准"与"齐"之间取舍。
    缺省权重为 0，即默认不把参与度算进分数（见 models 常量区说明）。
    """
    if expected <= 0:
        return Fraction(0, 1)
    return Fraction(max(min(int(participated), int(expected)), 0), int(expected)).reduced()


def composite_score(correct: int, valid_questions: int,
                    speed_sum: Fraction, answered_correct_rounds: int,
                    weight_accuracy: int, weight_speed: int,
                    participation: Fraction | None = None,
                    weight_participation: int = 0) -> Fraction:
    """个人综合分（0–100 的精确分数）。

    = (w_acc×正确率 + w_speed×平均速度系数 + w_part×参与度) ÷ 权重之和 × 100

    平均速度系数只在**答对的题**上取平均：没答对的题在正确率项已经扣过。
    参与度缺省不参与（权重 0），此时公式与两维版本完全一致。
    """
    w_acc = max(int(weight_accuracy), 0)
    w_speed = max(int(weight_speed), 0)
    w_part = max(int(weight_participation), 0)
    part = participation if (participation is not None and w_part > 0) else Fraction(0, 1)
    if w_part <= 0:
        part = Fraction(0, 1)
    total = w_acc + w_speed + w_part
    if total <= 0:
        # 权重全为 0：退化为只看正确率，而不是抛错或返回 0 分（现场不能因为填表失误而全员零分）
        return Fraction(correct * 100, max(valid_questions, 1))
    acc = accuracy_of(correct, valid_questions)
    avg_speed = (Fraction(speed_sum.num, speed_sum.den * answered_correct_rounds).reduced()
                 if answered_correct_rounds > 0 else Fraction(0, 1))
    # (w_acc×acc + w_speed×avg + w_part×part) / total × 100
    num = (w_acc * acc.num * avg_speed.den * part.den
           + w_speed * avg_speed.num * acc.den * part.den
           + w_part * part.num * acc.den * avg_speed.den) * 100
    den = total * acc.den * avg_speed.den * part.den
    # 量化到两位小数网格：与对外展示精度一致，同时保证能落进 int32 列（见 quantized 说明）
    return Fraction(num, max(den, 1)).quantized()


def scoring_mode_note(mode: str, weight_accuracy: int, weight_speed: int,
                      competition_mode: str = "shared_buzz",
                      weight_participation: int = 0) -> str:
    """面向学生的口径说明。数字必须与 composite_score 的实现一致。

    **这是要直接显示给学生看的字符串，不是 Markdown。**
    不要在这里写 `**加粗**`：调用方（结果页、大屏、后台）都是按纯文本渲染的，
    星号会原样出现在投影上。要强调就换措辞，不要换标记。
    （本项目实测踩到：大屏口径条上出现"速度按**同一题上全场最快答对者**为基准折算"。）
    """
    if mode == "composite":
        w_acc = max(int(weight_accuracy), 0)
        w_speed = max(int(weight_speed), 0)
        w_part = max(int(weight_participation), 0)
        total = w_acc + w_speed + w_part
        if total <= 0:
            return "学院得分＝本院作答的正确率"
        acc_pct = round(w_acc * 100 / total)
        part_pct = round(w_part * 100 / total)
        speed_pct = 100 - acc_pct - part_pct
        speed_label = "用时" if competition_mode == "relay" else "抢答速度"
        # 参与度只在实际启用时写进口径，避免给学生一条"有这一项但权重 0"的噪音说明
        part_text = (f"、参与度 {part_pct}%（本院实际上场人数占冻结名单的比例）"
                     if w_part > 0 else "")
        speed_text = (f"{speed_label} {speed_pct}%" if speed_pct > 0 else "")
        if not speed_text and not part_text:
            return "学院得分＝本院作答的正确率"
        detail = {
            "relay": "用时含做完全部题目的总用时（以最快完成的学院为基准折算），每题用时同时公布在结果页供复盘。",
            "shared_buzz": "速度以同一题上全场最快答对者为基准折算，答得越快得分越高；速度只在答对的题上计入。",
        }.get(competition_mode, "")
        return (f"学院得分＝正确率 {acc_pct}%"
                f"{'、' + speed_text if speed_text else ''}{part_text} 的加权综合评分；{detail}")
    if competition_mode == "relay":
        return "学院得分＝本院作答的正确率（只看答对比例；总用时与每题用时单独公布，不计入得分）"
    return "学院得分＝本院作答的正确率（只看答对比例，不计速度）"


def relay_time_factor(total_ms: int | None, fastest_ms: int | None) -> Fraction:
    """接力赛的时间系数（0–1）：以**最快完成全部题目的学院**为基准。

    与抢答的逐题速度系数同构（fastest / ours），区别只在分子分母是"整场总用时"。
    没有用时（该院没交完 / 缺数据）按 0 计。
    """
    if total_ms is None or fastest_ms is None:
        return Fraction(0, 1)
    reference = max(int(fastest_ms), 1)
    if total_ms <= 0:
        return Fraction(1, 1)
    return Fraction(reference, max(int(total_ms), 1))


def relay_completion_ms(answer_times: list[int | None]) -> int | None:
    """一个学院在某回合的完成用时 = 该院最后一份答卷相对本回合开放时刻的偏移。

    没有答卷时返回 None（= 没做），而不是 0（= 瞬间做完）：
    两者在时间系数上必须区分开，否则"没做"会拿到满速分。
    """
    vals = [t for t in answer_times if t is not None]
    return max(vals) if vals else None


def normalize_best_to_100(scores: list[Fraction]) -> list[Fraction]:
    """把一组原始分归一化为「最高者 = 100，其余按比例」。

    专用于总分制：原始总分与人数同量纲，不归一化就与 avg/accent 的 0–100 不可比，
    跨场也无法比较。全为 0 时原样返回 0（不做除零）。
    """
    if not scores:
        return []
    best = scores[0]
    for s in scores[1:]:
        if s.cmp(best) > 0:
            best = s
    if best.num == 0:
        return [Fraction(0, 1) for _ in scores]
    # s / best × 100 = (s.num × best.den × 100) / (s.den × best.num)
    # 同样量化到网格：这里也是"分母相乘"的高发地（见 quantized 说明）
    return [Fraction(s.num * best.den * 100, max(s.den * best.num, 1)).quantized()
            for s in scores]


def min_valid_questions(planned: int, ratio: float = 0.8) -> int:
    """赛前冻结的最低有效题数 = ceil(计划题数 × 比例)（§10.3）。"""
    return max(1, ceil(planned * ratio))


@dataclass
class HouseStanding:
    house_id: str
    house_key: str
    house_name: str
    participants: int
    correct_total: int
    score: Fraction
    meets_minimum: bool
    rank: int = 0
    is_tie: bool = False


def rank_standings(standings: list[HouseStanding]) -> list[HouseStanding]:
    """按精确分数排名（1224 制：同分并列同名次）。排序键用精确比较，不用浮点。"""
    ordered = sorted(standings, key=cmp_to_key(lambda a, b: b.score.cmp(a.score)))
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1].score.cmp(ordered[i].score) == 0:
            j += 1
        tie = j > i
        for k in range(i, j + 1):
            ordered[k].rank = i + 1
            ordered[k].is_tie = tie
        i = j + 1
    return ordered


def is_official_result(valid_questions: int, min_valid: int,
                       house_minimums: list[bool]) -> tuple[bool, str | None]:
    """满足正式排名条件判定（§4、§10.3）。返回 (is_official, 原因)。"""
    if valid_questions <= 0:
        return False, "无有效计分回合"
    if valid_questions < min_valid:
        return False, f"有效题数 {valid_questions} 低于赛前门槛 {min_valid}"
    if not all(house_minimums):
        return False, "存在 House 人数不满足赛前最低要求"
    return True, None
