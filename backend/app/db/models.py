"""全部数据模型（逻辑结构对应需求 §13.1，约束对应 §13.2）。

约定：
- 外键只建列级约束，不建 ORM relationship（异步下用显式查询，避免懒加载陷阱）。
- 枚举一律 String + 模块内常量，保证 SQLite/PostgreSQL 双兼容。
- 标准答案、判分细节只存在 question_versions / results 相关表，普通接口不外泄。
"""
from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, DateTimeTZ, String36, utcnow

# ── House ────────────────────────────────────────────────────────────────────


class House(Base):
    __tablename__ = "houses"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)
    name_cn: Mapped[str] = mapped_column(String(64))
    name_en: Mapped[str] = mapped_column(String(64))
    color_light: Mapped[str] = mapped_column(String(9), default="#EDF2EE")
    color_deep: Mapped[str] = mapped_column(String(9), default="#78877E")
    badge_asset: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)


# ── 账号与会话（§16）─────────────────────────────────────────────────────────

ROLE_ADMIN = "admin"
ROLE_ADVISOR = "advisor"
ROLE_STUDENT = "student"
# 「entrant（参赛）」角色已取消。它当初用来区分"只能练习"与"可被加入正式赛名单"，
# 实际效果是：学生注册后进不了比赛，还要管理员再单独加一次资格；管理员自己也被判成
# "正在比赛"，换台电脑就被拦。现行规则是**按学院参赛** —— 只要账号绑定了学院，
# 就能代表本学院上场，不需要任何额外资格。角色从此只表示"额外能力"
# （admin 能管理后台、advisor 能发起求援），不再表示"能不能参赛"。
LEGACY_ROLE_ENTRANT = "entrant"     # 仅用于读取历史数据与一次性迁移，不再写回数据库
# 仍然导出旧名字：老脚本、老测试、老数据里到处都是 `ROLE_ENTRANT`，
# 让它指向同一个字符串比让几十处 import 报错要诚实 —— 但业务代码不要再用它。
ROLE_ENTRANT = LEGACY_ROLE_ENTRANT
ROLES = (ROLE_ADMIN, ROLE_ADVISOR, ROLE_STUDENT)
# 旧值 → 新值的映射。管理端仍然接受 `entrant` 这个入参（老脚本、老书签、
# 管理员口述的"参赛账号"都会这么填），但落库一定是 student。
LEGACY_ROLE_ALIASES: dict[str, str] = {LEGACY_ROLE_ENTRANT: ROLE_STUDENT}
ROLE_LABELS: dict[str, str] = {ROLE_ADMIN: "管理员", ROLE_ADVISOR: "智囊", ROLE_STUDENT: "学生"}


def normalize_role(role: str | None) -> str | None:
    """把历史角色值折算成现行角色；None 原样返回（表示"不改动"）。"""
    if role is None:
        return None
    return LEGACY_ROLE_ALIASES.get(role, role)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_STUDENT)
    house_id: Mapped[str | None] = mapped_column(String36, ForeignKey("houses.id"), nullable=True)
    active_session_id: Mapped[str | None] = mapped_column(String36, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/disabled
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)

    # ── 自有注册与 House 归属 ──────────────────────────────────────────────
    # 学生自助注册时可以自选 House，**注册后自己不能改**
    # （前端不提供入口，后端也拒绝：`/auth/me` 里没有任何修改 House 的通道）。
    # 只有管理员能经 `/admin/users/{id}` 调整。`house_locked` 记录这一约定的来源，
    # 便于后台区分"学生自选"与"管理员指定"。
    self_registered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    house_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    # 封禁：status=disabled + 原因与时间（后台可见，学生看到中性提示）
    disabled_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    disabled_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    last_login_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)


class RuntimeSetting(Base):
    """运行期可变配置（后台开关）。

    与 `core/config.py` 的区分：那里是**部署配置**（环境变量，改了要重启）；
    这里是**学校日常运营要拨的开关**（是否允许注册、邀请码等），必须立即生效且留审计。
    键值都存字符串，读取时由业务层转类型——避免为每个开关加一列。
    """

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_by: Mapped[str | None] = mapped_column(String36, nullable=True)
    updated_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    user_id: Mapped[str] = mapped_column(String36, ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)
    expires_at: Mapped[str] = mapped_column(DateTimeTZ)
    revoked_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    last_seen_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)


class ControlLease(Base):
    """答题控制权凭据（服务端签发，仅一个活跃租约/用户；§16.1）。"""

    __tablename__ = "control_leases"

    user_id: Mapped[str] = mapped_column(String36, ForeignKey("users.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(String36)
    token: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)


# ── 题库（§13.3）────────────────────────────────────────────────────────────

POOL_OFFICIAL = "official"
POOL_PRACTICE = "practice"


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    difficulty: Mapped[int] = mapped_column(Integer, default=1)
    language: Mapped[str] = mapped_column(String(8), default="zh")
    pool: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/published/retired
    current_version_id: Mapped[str | None] = mapped_column(String36, nullable=True)
    prompt_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String36, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)


class QuestionVersion(Base):
    __tablename__ = "question_versions"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    question_id: Mapped[str] = mapped_column(String36, ForeignKey("questions.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    prompt: Mapped[str] = mapped_column(Text)
    options_json: Mapped[list] = mapped_column(JSON)  # [{option_id, text}]
    correct_option_id: Mapped[str] = mapped_column(String(16))
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    copyright_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    image_asset_id: Mapped[str | None] = mapped_column(String36, nullable=True)
    review_state: Mapped[str] = mapped_column(String(16), default="pending")  # pending/approved/rejected
    created_by: Mapped[str | None] = mapped_column(String36, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)

    __table_args__ = (UniqueConstraint("question_id", "version_no", name="uq_question_versions_q_ver"),)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    path: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    visibility: Mapped[str] = mapped_column(String(16), default="protected")  # public/protected
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)


# ── 比赛与名单（§13.1）───────────────────────────────────────────────────────

MATCH_DRAFT = "draft"
MATCH_LOBBY = "lobby"
MATCH_RUNNING = "running"
MATCH_PAUSED = "paused"
MATCH_LOCKED = "locked"
MATCH_CALCULATED = "calculated"
MATCH_REVEAL_SCHEDULED = "reveal_scheduled"
MATCH_REVEALED = "revealed"
MATCH_ARCHIVED = "archived"
MATCH_CANCELLED = "cancelled"

MODE_FIXED_K = "fixed_k"
MODE_OPEN = "open"

# ── 题源（后台建赛时选择）──────────────────────────────────────────────────
#   random    随机：从已审核的正式池随机抽（可按难度配比约束）
#   custom    自定义：管理员逐题指定（校验必须属于已审核的正式池）
#   blueprint 主题配额：按主题/难度配额抽，兼顾覆盖面
SRC_RANDOM = "random"
SRC_CUSTOM = "custom"
SRC_BLUEPRINT = "blueprint"
QUESTION_SOURCES = (SRC_RANDOM, SRC_CUSTOM, SRC_BLUEPRINT)

# ── 评分标准（后台建赛时选择，决定学院官方分怎么算）───────────────────────
#   avg       人均平均分（现行 R19）：100 × 全院答对总数 ÷ (上场人数 × 有效题数)
#             等价于「成员个人分的算术平均」。人多人少不影响量纲。
#   total     总分累加：100 × 全院答对总数 ÷ 有效题数。人多占优，适合鼓励全员参与。
#   top_n     前 N 名平均：取本院个人分最高的 N 人求平均（N 默认 5）。
#             抑制人海战术，适合"不限人数赛"。
#   accuracy  正确率：100 × 全院答对总数 ÷ 全院作答总数。只看准度不看参与量。
#
# ⚠️ 任何评分标准都要求**四院使用同一套**（否则不同学院之间不可比）。这是系统不变量：
#    评分标准存在 match 上，天然对全院生效。
# ── 赛制（双模式，后台开启竞赛时选择）─────────────────────────────────────
#   shared_buzz 抢答：**一场竞赛一个大题库**，四院同时看同一道题、同时抢答，
#               答对且更快的人/学院得更高分。题目进度由控制器统一推进（一回合一道题）。
#   relay       接力：**每个 House 固定题目数量**（题库按院均分，同轮各院做不同的题），
#               从开赛到交完自己那份题的**总用时**与**每题用时**进入评分标准；
#               支持镜像回放（按作答时间线重放整场）。
#
# 为什么是"两种"而不是"参数组合"：这两种赛制在**回合语义**上就不同——
#   抢答是"同一时刻同一道题"（进度必须全局同步，否则抢答无意义）；
#   接力是"同一时刻不同题、各院自己控节奏"（进度按院推进，快慢才是比的东西）。
# 混在一起做会出现"某院还在第 3 题、另一院已经第 9 题"却要求全局同步的矛盾。
COMP_MODE_BUZZ = "shared_buzz"
COMP_MODE_RELAY = "relay"
COMPETITION_MODES = (COMP_MODE_BUZZ, COMP_MODE_RELAY)
# 建库时种下的四个学院 key（与 scripts/seed.py 一致）。
# 用途：接力赛在建赛时就要知道"全场题量 = 每院题量 × 几个学院"，
# 而那一刻 houses 表可能还没被查过；这里作为**默认范围**，实际参赛范围以 houses_scope 为准。
ACTIVE_HOUSE_KEYS = ("tiger", "dragon", "tortoise", "phoenix")

SCORE_AVG = "avg"
SCORE_TOTAL = "total"
SCORE_TOP_N = "top_n"
SCORE_ACCURACY = "accuracy"
SCORING_METHODS = (SCORE_AVG, SCORE_TOTAL, SCORE_TOP_N, SCORE_ACCURACY)

# ── 评分模式与权重（后台可配、赛前冻结、结果页标注）────────────────────────
#   pure      只看正确率：学院分 = 100 × 答对 ÷ 有效题数（与 avg 同口径）。
#             最"干净"的口径，适合希望把注意力放在知识本身的场次。
#   composite 综合评估：正确率与作答速度按权重合成，
#             学院分 = (w_acc × 正确率 + w_speed × 速度分) ÷ (w_acc + w_speed)。
#             速度分按**抢答用时**逐回合计算（见 competitions.rules.composite_score），
#             目的是让"抢答"这种赛制真的比"想得久"更值钱。
#
# 权重用整数（0–1000）存储：避免浮点在跨进程/跨库上出现"看着一样实际不等"的问题，
# 计算时再当作有理数的分母用，全程精确。
SCORE_MODE_PURE = "pure"
SCORE_MODE_COMPOSITE = "composite"
SCORING_MODES = (SCORE_MODE_PURE, SCORE_MODE_COMPOSITE)
DEFAULT_WEIGHT_ACC = 700
DEFAULT_WEIGHT_SPEED = 300
# 接力赛的"速度"就是**总用时与每题用时**，因此单给一个时间权重，
# 与抢答共用同一套权重合成公式（见 rules.composite_score）。
DEFAULT_WEIGHT_TIME = 300
# 参与度权重：缺省 0，即"不把参与度算进分数"。
#
# 为什么默认 0 而不是像正确率/速度那样给个非零默认值：
# 参与度（本院提交人数覆盖率）与"人均平均"口径天然冲突 —— 一个人都没交的学院参与度是 0，
# 但它的正确率也是 0，两者会叠加成双重惩罚；而"鼓励全员上线"是该场次的组织意图，
# 不该由系统替学校默认决定。因此它是**显式开启**的第三个维度：
#   参与度 = 本院实际提交人数 ÷ 本院应参赛人数（冻结名单人数）
# 与正确率同量纲（都是比例），因此可以直接加权合成，不需要额外归一化。
DEFAULT_WEIGHT_PARTICIPATION = 0

# 赛中可见性三档（后台可配，默认 house）：
#   off        — 只广播聚合进度（各院已交人数/已完回合）
#   house      — 额外广播各院当前平均分与正确率
#   individual — 再额外广播匿名别名的个人已交状态与名次段位
VIS_OFF = "off"
VIS_HOUSE = "house"
VIS_INDIVIDUAL = "individual"
LIVE_VISIBILITIES = (VIS_OFF, VIS_HOUSE, VIS_INDIVIDUAL)

# ── 观战模式（后台可开关，决定观众看到多少）───────────────────────────────
#   mirror   镜像：和参赛者一模一样的画面（题干 + 选项 + 倒计时 + 作答态）
#   blur     模糊题：版式与节奏一致，但题干与选项做模糊，只有进度是真的
#   summary  仅进度：只给本院与全场的提交进度，不出现题目区域
#
# 安全性判断（这是为什么 mirror 可以开）：
#   · 观战已被限定为"只能看自己 House 的比赛"（未揭晓前），因此观众本身就是该场选手之一，
#     他本来就能在答题页完整看到题目与选项 —— mirror 不构成任何额外泄露；
#   · 真正需要防的是**院外人员提前看到题目**，那由"未揭晓只能看本院"这条红线挡住；
#   · blur / summary 是为更保守的场合准备的（例如观众席有大屏、或允许非选手观战）。
SPECTATE_MIRROR = "mirror"
SPECTATE_BLUR = "blur"
SPECTATE_SUMMARY = "summary"
SPECTATE_MODES = (SPECTATE_MIRROR, SPECTATE_BLUR, SPECTATE_SUMMARY)


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16), default=MODE_FIXED_K)
    status: Mapped[str] = mapped_column(String(24), default=MATCH_DRAFT, index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)

    planned_questions: Mapped[int] = mapped_column(Integer, default=20)
    fixed_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_house_size: Mapped[int] = mapped_column(Integer, default=1)
    min_valid_questions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 赛制：抢答（shared_buzz）或接力（relay），见上方常量区说明。
    competition_mode: Mapped[str] = mapped_column(String(16), default=COMP_MODE_BUZZ)
    # 接力赛每院题目数量。抢答赛忽略此项（题量看 planned_questions）。
    questions_per_house: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 接力赛的时间权重（0–1000）：总用时与每题用时按此权重进入综合评分
    weight_time: Mapped[int] = mapped_column(Integer, default=DEFAULT_WEIGHT_TIME)

    # 建赛时选择的题源与评分标准（见上方常量区说明）。
    # 题源为 custom 时，具体题目在 match_questions 里冻结；这里只记「当初是怎么选的」，
    # 便于首页与结果页向学生说明口径。
    question_source: Mapped[str] = mapped_column(String(16), default=SRC_RANDOM)
    scoring_method: Mapped[str] = mapped_column(String(16), default=SCORE_AVG)
    scoring_top_n: Mapped[int] = mapped_column(Integer, default=5)   # 仅 top_n 生效
    # 评分模式与权重（见上方常量区说明）。
    # 这两项与 scoring_method 一样属于**赛前冻结的计分口径**：开赛后不允许改，
    # 改了就等于把已经打过的回合按新规则重算，学院之间立刻不可比。
    scoring_mode: Mapped[str] = mapped_column(String(16), default=SCORE_MODE_PURE)
    weight_accuracy: Mapped[int] = mapped_column(Integer, default=DEFAULT_WEIGHT_ACC)    # 0–1000
    weight_speed: Mapped[int] = mapped_column(Integer, default=DEFAULT_WEIGHT_SPEED)     # 0–1000
    # 参与度权重（0–1000）。0 = 不计入（默认）。
    weight_participation: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_WEIGHT_PARTICIPATION)
    # 难度配比（随机/配额抽题时约束用）：{"1": n, "2": n, "3": n}；空 = 不限
    difficulty_mix: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 参战学院范围：["tiger","dragon",...]；空 = 四院全参。
    # 用途：允许"两院对抗赛"这类非四院同场的比赛。
    # 约束：**选手只能加入包含自己学院的那一场**（服务端强制，见 check_in），
    # 未揭晓前观战同理——不属于该场的学院看不到该场的任何进度。
    houses_scope: Mapped[list | None] = mapped_column(JSON, nullable=True)

    display_seconds: Mapped[int] = mapped_column(Integer, default=10)
    answer_seconds: Mapped[int] = mapped_column(Integer, default=20)
    interlude_seconds: Mapped[int] = mapped_column(Integer, default=5)

    aid_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    aid_window_seconds: Mapped[int] = mapped_column(Integer, default=8)
    aid_quota_per_house: Mapped[int] = mapped_column(Integer, default=1)

    # 赛中可见性（§8）：off / house / individual，见上方常量说明
    live_visibility: Mapped[str] = mapped_column(String(16), default=VIS_HOUSE)
    # 观战模式：mirror / blur / summary，见上方常量说明（默认镜像，最直观）
    spectator_mode: Mapped[str] = mapped_column(String(16), default=SPECTATE_MIRROR)

    rules_version: Mapped[str] = mapped_column(String(16), default="1")
    current_round_id: Mapped[str | None] = mapped_column(String36, nullable=True)
    # 预定开赛时间。与 ceremony/reveal 一样是**面向展示**的时间：
    # 首页在 lobby 阶段据此显示倒计时，到点由主持人执行 start_match 真正开赛。
    # 它不是权威时序（真正的截止判定仍以各回合的服务端时间为准），因此允许晚到/早到。
    start_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    ceremony_start_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    reveal_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    published_result_version_id: Mapped[str | None] = mapped_column(String36, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String36, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)


class MatchEligibility(Base):
    __tablename__ = "match_eligibilities"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    user_id: Mapped[str] = mapped_column(String36, ForeignKey("users.id"), index=True)
    approved_by: Mapped[str | None] = mapped_column(String36, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)

    __table_args__ = (UniqueConstraint("match_id", "user_id", name="uq_elig_match_user"),)


class MatchEntry(Base):
    __tablename__ = "match_entries"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    user_id: Mapped[str] = mapped_column(String36, ForeignKey("users.id"))
    house_id: Mapped[str] = mapped_column(String36, ForeignKey("houses.id"))
    house_key_snapshot: Mapped[str] = mapped_column(String(32))
    house_name_snapshot: Mapped[str] = mapped_column(String(64))
    display_alias: Mapped[str] = mapped_column(String(32))
    is_captain: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_in_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    scoring_status: Mapped[str] = mapped_column(String(16), default="active")  # active/excluded
    exclusion_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)

    __table_args__ = (UniqueConstraint("match_id", "user_id", name="uq_entries_match_user"),)


class MatchQuestion(Base):
    __tablename__ = "match_questions"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    question_id: Mapped[str] = mapped_column(String36, ForeignKey("questions.id"))
    version_id: Mapped[str] = mapped_column(String36, ForeignKey("question_versions.id"))
    position: Mapped[int] = mapped_column(Integer)
    is_backup: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("match_id", "question_id", name="uq_match_questions_mq"),
        Index("ix_match_questions_order", "match_id", "is_backup", "position"),
    )


ROUND_PENDING = "pending"
ROUND_DISPLAYING = "displaying"
ROUND_OPEN = "open"
ROUND_CLOSED = "closed"
ROUND_VOID = "void"


class RoundInstance(Base):
    __tablename__ = "round_instances"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    match_question_id: Mapped[str] = mapped_column(String36, ForeignKey("match_questions.id"))
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default=ROUND_PENDING)
    display_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    open_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    deadline_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    closed_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 接力赛：本回合只归哪个学院作答（["dragon"]）。空 = 四院同题（抢答）。
    # 这一列把"每个 House 固定题目数量"落进既有的回合模型里，不必再引入第二套题目分配表。
    house_scope: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)

    __table_args__ = (UniqueConstraint("match_id", "match_question_id", "attempt_no", name="uq_rounds_mq_attempt"),)


# ── 答卷（§10.2）────────────────────────────────────────────────────────────


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    round_instance_id: Mapped[str] = mapped_column(String36, ForeignKey("round_instances.id"), index=True)
    entry_id: Mapped[str] = mapped_column(String36, ForeignKey("match_entries.id"))
    option_id: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[str] = mapped_column(DateTimeTZ)
    committed_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)
    receipt_id: Mapped[str] = mapped_column(String(24), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    # 服务端计算的作答用时（received_at − round.open_at），毫秒。
    # 仅统计用，不参与积分；历史数据为 NULL，展示时按「无样本」处理而不是记 0 秒。
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("round_instance_id", "entry_id", name="uq_answers_round_entry"),
        UniqueConstraint("match_id", "entry_id", "idempotency_key", name="uq_answers_idem"),
        Index("ix_answers_speed", "match_id", "elapsed_ms"),
    )


# ── 场外援助（§5）────────────────────────────────────────────────────────────


class MatchHouseState(Base):
    __tablename__ = "match_house_state"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    house_id: Mapped[str] = mapped_column(String36, ForeignKey("houses.id"))
    aid_remaining: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (UniqueConstraint("match_id", "house_id", name="uq_mhs_match_house"),)


AID_PENDING = "pending"
AID_ANSWERED = "answered"
AID_EXPIRED = "expired"
AID_REFUNDED = "refunded"


class AidRequest(Base):
    __tablename__ = "aid_requests"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    round_instance_id: Mapped[str] = mapped_column(String36, ForeignKey("round_instances.id"))
    house_id: Mapped[str] = mapped_column(String36, ForeignKey("houses.id"))
    requested_by_entry_id: Mapped[str] = mapped_column(String36, ForeignKey("match_entries.id"))
    status: Mapped[str] = mapped_column(String(16), default=AID_PENDING)
    advice_option_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    advice_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    advised_by_user_id: Mapped[str | None] = mapped_column(String36, nullable=True)
    advised_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)

    __table_args__ = (UniqueConstraint("round_instance_id", "house_id", name="uq_aid_round_house"),)


# ── 成绩与版本（§15.3）───────────────────────────────────────────────────────


class ResultVersion(Base):
    __tablename__ = "result_versions"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    algorithm_version: Mapped[str] = mapped_column(String(8), default="1")
    valid_question_count: Mapped[int] = mapped_column(Integer, default=0)
    is_official: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    calculated_by: Mapped[str | None] = mapped_column(String36, nullable=True)
    calculated_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)
    published_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)

    __table_args__ = (UniqueConstraint("match_id", "version_no", name="uq_results_match_ver"),)


class ParticipantResult(Base):
    __tablename__ = "participant_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    result_version_id: Mapped[str] = mapped_column(String36, ForeignKey("result_versions.id"), index=True)
    entry_id: Mapped[str] = mapped_column(String36, ForeignKey("match_entries.id"))
    user_id: Mapped[str] = mapped_column(String36, ForeignKey("users.id"))
    house_id: Mapped[str] = mapped_column(String36, ForeignKey("houses.id"))
    correct_count: Mapped[int] = mapped_column(Integer, default=0)
    answered_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_question_count: Mapped[int] = mapped_column(Integer, default=0)
    score_num: Mapped[int] = mapped_column(Integer, default=0)
    score_den: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (UniqueConstraint("result_version_id", "entry_id", name="uq_pr_ver_entry"),)


class HouseResult(Base):
    __tablename__ = "house_results"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    result_version_id: Mapped[str] = mapped_column(String36, ForeignKey("result_versions.id"), index=True)
    house_id: Mapped[str] = mapped_column(String36, ForeignKey("houses.id"))
    house_key_snapshot: Mapped[str] = mapped_column(String(32))
    house_name_snapshot: Mapped[str] = mapped_column(String(64))
    participant_count: Mapped[int] = mapped_column(Integer, default=0)
    correct_total: Mapped[int] = mapped_column(Integer, default=0)
    score_num: Mapped[int] = mapped_column(Integer, default=0)
    score_den: Mapped[int] = mapped_column(Integer, default=1)
    rank: Mapped[int] = mapped_column(Integer, default=1)
    is_tie: Mapped[bool] = mapped_column(Boolean, default=False)
    meets_minimum: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("result_version_id", "house_id", name="uq_hr_ver_house"),)


class HouseTimeResult(Base):
    """接力赛的用时成绩（每学院一行，随成绩版本冻结）。

    为什么单独一张表：接力赛的评分标准里有"全部题目做完的总用时"和"每题用时"，
    它们是**计分依据**（综合评估模式直接进入分数），必须与成绩版本一起不可变、
    一起可追溯；塞进 notes 那种给人看的文本里会让口径无法被程序核对。

    单位一律毫秒（整数），与 `answers.elapsed_ms` 同源，避免秒/毫秒混用。
    `time_factor_permille` 存的是"以最快学院为基准"的时间系数 ×1000：
    用整数存是为了跨库比较精确（0.667 这类小数在浮点里不可靠）。
    """
    __tablename__ = "house_time_results"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    result_version_id: Mapped[str] = mapped_column(String36, ForeignKey("result_versions.id"), index=True)
    house_id: Mapped[str] = mapped_column(String36, ForeignKey("houses.id"))
    house_key_snapshot: Mapped[str] = mapped_column(String(32))
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_question_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answered_rounds: Mapped[int] = mapped_column(Integer, default=0)
    expected_rounds: Mapped[int] = mapped_column(Integer, default=0)
    time_factor_permille: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("result_version_id", "house_id", name="uq_htr_ver_house"),)


# ── 事件、命令与审计（§10.2）─────────────────────────────────────────────────


class MatchEvent(Base):
    __tablename__ = "match_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)


class AdminCommand(Base):
    __tablename__ = "admin_commands"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    command_id: Mapped[str] = mapped_column(String(64), unique=True)
    match_id: Mapped[str] = mapped_column(String36, ForeignKey("matches.id"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    expected_state_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(String(8))  # ok/rejected
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(String36, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str | None] = mapped_column(String36, nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String36, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)


# ── 练习（独立于正式成绩，§13.1）────────────────────────────────────────────


class PracticeSession(Base):
    __tablename__ = "practice_sessions"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    # 登录用户与匿名访客二选一：登录时填 user_id；匿名时 user_id 为空、填 guest_id
    user_id: Mapped[str | None] = mapped_column(
        String36, ForeignKey("users.id"), index=True, nullable=True)
    guest_id: Mapped[str | None] = mapped_column(
        String36, ForeignKey("guest_identities.id"), index=True, nullable=True)
    subject: Mapped[str | None] = mapped_column(String(32), nullable=True)
    question_ids: Mapped[list] = mapped_column(JSON)  # question_version_id 列表
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/finished
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)
    finished_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)

    # ── 结算与排行榜（练习成绩与正式赛成绩物理分区，绝不混排）──
    answered_count: Mapped[int] = mapped_column(Integer, default=0)
    correct_count: Mapped[int] = mapped_column(Integer, default=0)
    score_num: Mapped[int] = mapped_column(Integer, default=0)   # 正确率，精确分数 ×10000
    score_den: Mapped[int] = mapped_column(Integer, default=1)
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 整场用时
    best_streak: Mapped[int] = mapped_column(Integer, default=0)           # 最长连对
    is_ranked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 上榜展示名。NULL → 显示默认匿名名「枫禾学子」；不存真实姓名/学号
    display_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    house_id: Mapped[str | None] = mapped_column(String36, ForeignKey("houses.id"), nullable=True)


class GuestIdentity(Base):
    """匿名学习者身份。令牌只存散列；显示序号稳定绑定本行，不含任何个人信息。"""

    __tablename__ = "guest_identities"

    id: Mapped[str] = mapped_column(String36, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    display_seq: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)
    last_seen_at: Mapped[str | None] = mapped_column(DateTimeTZ, nullable=True)


class PracticeAnswer(Base):
    __tablename__ = "practice_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String36, ForeignKey("practice_sessions.id"), index=True)
    question_version_id: Mapped[str] = mapped_column(String36, ForeignKey("question_versions.id"))
    option_id: Mapped[str] = mapped_column(String(16))
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    answered_at: Mapped[str] = mapped_column(DateTimeTZ, default=utcnow)
    # 服务端计算的本题用时（服务端受理时间 − 出题时间），毫秒；速度榜用
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (UniqueConstraint("session_id", "question_version_id", name="uq_pa_sess_qv"),)


# 备用：速度统计后续扩展列（float 秒），首版仅保留占位说明
FloatPlaceholder = Float
