from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """环境配置。所有变量可用 MGUS_ 前缀覆盖，例如 MGUS_DATABASE_URL。"""

    model_config = SettingsConfigDict(env_prefix="MGUS_", env_file=".env", extra="ignore")
    app_name: str = "MGUS House Quiz"
    database_url: str = "sqlite+aiosqlite:///./mgus.db"
    session_ttl_hours: int = 12
    transfer_code_ttl_minutes: int = 10
    cookie_name: str = "mgus_session"
    cookie_secure: bool = False          # 生产（HTTPS）必须为 true
    cookie_domain: str | None = None
    # CSRF：双提交令牌（非 HttpOnly Cookie ↔ X-CSRF-Token 头）。
    # 学校场景下所有人共用出口 IP，因此 IP 只做宽松辅助，绝不作为主要限流键。
    csrf_cookie_name: str = "mgus_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    # `NoDecode` 是关键：pydantic-settings 默认会把 list 字段的环境变量当 JSON 解析，
    # 而且**在 field_validator(mode="before") 之前**就解析了 —— 于是填普通 URL 会在
    # 解码阶段直接抛 SettingsError，validator 根本没机会跑（实测确认）。
    # 标上 NoDecode 后原始字符串会原样交给下面的 validator，由我们自己解析。
    allowed_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173", "http://127.0.0.1:5173",
    ]

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _parse_origins(cls, v):
        """接受**三种**写法，因为它们都会真实出现在部署现场：

          ① 逗号分隔（最自然，docker-compose 与 .env 里都这么写）
             MGUS_ALLOWED_ORIGINS=https://quiz.example.edu,http://10.0.0.8
          ② JSON 数组（pydantic-settings 对 list 字段的默认要求）
             MGUS_ALLOWED_ORIGINS=["https://quiz.example.edu"]
          ③ 单个 URL（只有一个来源时最省事）
             MGUS_ALLOWED_ORIGINS=https://quiz.example.edu

        为什么必须做这个兼容：照 docker-compose.yml 里的注释填一个普通 URL 时，
        导入期会抛 `SettingsError: error parsing value for field "allowed_origins"`，
        **API 直接起不来** —— 也就是按文档部署必定失败（本项目实测踩到）。
        放宽解析而不是改文档：填一个网址是任何人的第一直觉，拦它没有收益。
        """
        if v is None or isinstance(v, list):
            return v
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except ValueError:
                    pass  # 落回逗号分隔解析
            return [p.strip() for p in s.split(",") if p.strip()]
        return v

    broadcast_interval_s: float = 1.0    # 常规进度广播间隔（§14.2）
    round_tick_s: float = 0.4            # 比赛控制器检查周期
    answer_drain_budget_s: float = 3.0   # 回合关闭后排空在途请求的预算（§10.2）

    # 读题窗口秒数。**0 = 抢答**（本系统的正式赛制：题目一出现即可作答，读题与作答同一段
    # 时间）。>0 时保留"先看后答"，用于主持人念题等场次。可在建赛时逐场覆盖。
    default_display_seconds: int = 0
    default_answer_seconds: int = 20     # 每题作答
    default_interlude_seconds: int = 5   # 题间过渡
    default_aid_window_seconds: int = 8  # 求援窗口（展示期前段）
    default_min_house_size: int = 1      # 不限人数赛每 House 最低人数
    min_valid_question_ratio: float = 0.8  # 最低有效题数 = ceil(计划题数×比例)（§10.3）

    # 会话活跃度：超过这个秒数没有任何请求，就认为原设备已经不用了，
    # 换设备时**直接放行**、不再要求一次性交接码。
    # 产品要求（现场口径）："超过 1 分钟没有答题，活跃就可以登"。
    handover_idle_seconds: int = 60

    rate_limit_login_per_min: int = 10
    # 同一账号连续失败上限（成功即清零）；超限锁定，指数不需要——现场有主持人可即时处置
    rate_limit_login_fail_per_min: int = 5
    # 同一出口 IP 的宽松上限：全校共用出口，取足够大的值只为拦住脚本
    rate_limit_login_ip_per_min: int = 120
    rate_limit_submit_per_min: int = 40
    # 匿名练习：同一访客 token 每分钟可提交的题目数
    rate_limit_practice_per_min: int = 45
    bcrypt_rounds: int = 12

    protocol_version: str = "1"
    seed_admin_username: str = "admin"
    seed_admin_password: str = "change-me-now"  # 种子后必须修改


@lru_cache
def get_settings() -> Settings:
    return Settings()
