"""运行期设置（后台开关）。

与 `core/config.py` 的区别：那里是**部署配置**（环境变量，改动要重启进程）；
这里是**学校日常运营要拨的开关**——是否允许自助注册、邀请码等，必须立即生效并留审计。

设计取舍：键值统一按字符串存，读取时在 `_coerce` 里转类型。
不为每个开关加一列，是因为开关会随运营需要不断增加，而加列每次都要迁移。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import RuntimeSetting

# ── 已知键与默认值 ──────────────────────────────────────────────────────────
KEY_REGISTRATION_OPEN = "registration_open"
KEY_REGISTRATION_CODE = "registration_code"
KEY_REGISTRATION_DEFAULT_ROLE = "registration_default_role"
KEY_REGISTRATION_REVIEW = "registration_review"

DEFAULTS: dict[str, object] = {
    # 是否允许学生自助注册。默认**关闭**：学校通常先由管理员预建账号，
    # 需要开放时由管理员显式打开（避免上线当天被陌生人注册）。
    KEY_REGISTRATION_OPEN: False,
    # 可选邀请码。为空表示不需要；非空时注册必须带对。
    KEY_REGISTRATION_CODE: "",
    # 自助注册账号的默认角色。**只有「student」是合理的**：所有绑定了学院的账号
    # 都能代表本院参加正式赛（"entrant"这个角色已经取消，历史值会被折算成 student）。
    # 这个键保留下来只是为了不改动运行期设置的存储结构，正常运营不需要改它。
    KEY_REGISTRATION_DEFAULT_ROLE: "student",
    # 注册是否需要管理员审核。
    #   False（默认）→ 学生选完学院立即可以登录使用，最省事，适合校内开放注册；
    #   True         → 注册后账号为 pending 状态，**无法登录**，管理员在后台逐个通过后才可用。
    # 为什么默认 False：这台机器在校园网内，开放注册通常是老师盯着开的；
    # 若默认要审核，学生注册完发现登不进去会以为系统坏了。
    # 需要严格管控时由管理员显式打开。
    KEY_REGISTRATION_REVIEW: False,
}

Bool = bool
Int = int


def _coerce(key: str, raw: str):
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    return raw


async def get_all(db: AsyncSession) -> dict:
    """全部开关的当前值（含默认）。后台"设置"页直接渲染这个。"""
    rows = (await db.execute(select(RuntimeSetting))).scalars().all()
    stored = {r.key: r.value for r in rows}
    out = {}
    for k, dv in DEFAULTS.items():
        out[k] = _coerce(k, stored[k]) if k in stored else dv
    return out


async def get(db: AsyncSession, key: str):
    row = (await db.execute(select(RuntimeSetting).where(RuntimeSetting.key == key))).scalar_one_or_none()
    if row is None:
        return DEFAULTS.get(key)
    return _coerce(key, row.value)


async def set_value(db: AsyncSession, key: str, value, actor_id: str | None) -> None:
    """写入一个开关。未知键直接拒绝——避免拼错键名后静默失效。"""
    if key not in DEFAULTS:
        raise KeyError(f"未知设置项：{key}")
    raw = ("true" if value else "false") if isinstance(value, bool) else str(value)
    row = (await db.execute(select(RuntimeSetting).where(RuntimeSetting.key == key))).scalar_one_or_none()
    if row is None:
        db.add(RuntimeSetting(key=key, value=raw, updated_by=actor_id, updated_at=utcnow()))
    else:
        row.value = raw
        row.updated_by = actor_id
        row.updated_at = utcnow()
