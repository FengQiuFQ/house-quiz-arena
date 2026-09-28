"""认证与权限依赖（§16）。

三种身份分离：登录会话（Cookie）→ 本模块；
答题控制权（ControlLease，X-Control-Token）→ submissions 校验；
WebSocket 连接 → realtime 模块自行鉴权。
"""
from typing import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.security import token_hash
from app.db.base import as_utc, utcnow
from app.db.models import ROLES, ROLE_ADMIN, User, UserSession
from app.db.session import make_session_factory

_factory = make_session_factory()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with _factory() as session:
        yield session


def set_session_cookie(response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        key=s.cookie_name,
        value=token,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        max_age=s.session_ttl_hours * 3600,
        path="/",
        domain=s.cookie_domain,
    )


def clear_session_cookie(response) -> None:
    s = get_settings()
    response.delete_cookie(key=s.cookie_name, path="/", domain=s.cookie_domain)


# ── CSRF 双提交令牌（B1-E5）─────────────────────────────────────────────────
# 会话走 HttpOnly Cookie，任何写操作都必须带 X-CSRF-Token，其值等于**服务端签发的会话明文**：
# 跨站页面既能读 Cookie 也能设 Cookie，但**无法知道**会话明文，因此无法伪造该头。
# 相较另发一枚独立令牌，这样不需要额外存储；令牌仍只以散列形式落库（user_sessions.token_hash）。

def ensure_csrf_cookie(response, session_token: str) -> None:
    """登录/认领时下发可被 JS 读取的 CSRF Cookie（值＝会话令牌，非 HttpOnly）。"""
    s = get_settings()
    response.set_cookie(
        key=s.csrf_cookie_name,
        value=session_token,
        httponly=False,
        secure=s.cookie_secure,
        samesite="lax",
        max_age=s.session_ttl_hours * 3600,
        path="/",
        domain=s.cookie_domain,
    )


def clear_csrf_cookie(response) -> None:
    s = get_settings()
    response.delete_cookie(key=s.csrf_cookie_name, path="/", domain=s.cookie_domain)


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(get_settings().cookie_name)
    if not token:
        # 竞赛相关入口用 LOGIN_REQUIRED，让前端能区分"需要登录"与"会话过期"
        raise AppError(401, ErrorCode.LOGIN_REQUIRED, "请先登录后再进行比赛相关操作。")
    stmt = select(UserSession).where(UserSession.token_hash == token_hash(token))
    sess = (await db.execute(stmt)).scalar_one_or_none()
    now = utcnow()
    if sess is None:
        raise AppError(401, ErrorCode.UNAUTHORIZED, "未登录或登录已失效。")
    # 会话代次校验优先：被换机/核验吊销的旧代次令牌返回 SESSION_REPLACED（§14.1）
    user = await db.get(User, sess.user_id)
    if user is None or user.status != "active":
        raise AppError(401, ErrorCode.UNAUTHORIZED, "账号不可用。")
    if user.active_session_id != sess.id:
        raise AppError(401, ErrorCode.SESSION_REPLACED, "会话已在其他设备接管，本设备已失效。")
    if sess.revoked_at is not None or as_utc(sess.expires_at) < as_utc(now):
        raise AppError(401, ErrorCode.UNAUTHORIZED, "登录已失效，请重新登录。")
    request.state.session = sess
    request.state.session_id = sess.id

    # 刷新"最后活跃时间"（换机判定的依据）。
    #
    # 这一行以前**缺失**：`last_seen_at` 只在建会话时写一次、之后永不更新，
    # 于是"这个设备还在用吗"根本无从判断 —— 换机规则只能退化成"只要会话没吊销就一直占着"，
    # 现场表现为"我明明已经没在答题了，去别的设备登录还被要求出示交接码"。
    #
    # 只在超过 _HEARTBEAT_S 秒才写库：答题页每秒都在轮询快照，
    # 每请求一次 UPDATE 会把会话表打成热行（500 人同场时是每秒 500 次无意义写入）。
    if sess.last_seen_at is None or \
            (as_utc(now) - as_utc(sess.last_seen_at)).total_seconds() >= _HEARTBEAT_S:
        sess.last_seen_at = now
        await db.commit()

    # 首登/重置后强制改密（B1-E7）：默认口令（种子 admin 为 change-me-now）不得长期存活。
    if user.must_change_password and not _password_change_exempt(request):
        raise AppError(403, ErrorCode.PASSWORD_CHANGE_REQUIRED,
                       "首次登录或口令被重置后必须先修改密码。",
                       {"must_change_password": True})
    return user


# 改密前仍可使用的接口：只有这几条，其余一律 403 PASSWORD_CHANGE_REQUIRED
_PW_EXEMPT_PATHS = ("/api/v1/auth/change-password", "/api/v1/auth/me",
                    "/api/v1/auth/logout", "/healthz", "/readyz")

# last_seen_at 的写库节流：同一会话在此秒数内的重复请求不再写库（见 get_current_user）
_HEARTBEAT_S = 30.0


def _password_change_exempt(request) -> bool:
    if request.method.upper() in ("GET", "HEAD", "OPTIONS"):
        return True
    path = request.url.path.rstrip("/") or "/"
    return any(path == p.rstrip("/") for p in _PW_EXEMPT_PATHS)


async def get_optional_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    try:
        return await get_current_user(request, db)
    except AppError:
        return None


def require_role(*roles: str):
    if not set(roles) <= set(ROLES):
        raise ValueError("未知角色")

    async def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise AppError(403, ErrorCode.FORBIDDEN, "权限不足。")
        return user

    return _dep


require_admin = require_role(ROLE_ADMIN)


async def require_house_member(user: User = Depends(get_current_user)) -> User:
    """参赛类操作的统一门槛：**必须登录，且必须已绑定 House**。

    为什么单独做一个依赖而不是散在各处判断：
      · 未绑定 House 的账号无法计入任何学院分，若放行会产生"匿名幽灵选手"——
        他的答题既不给学院加分，也不出现在任何统计里，学生只会觉得系统坏了。
      · 前端的按钮禁用不能作为权限控制（可直接调接口），因此这是服务端硬门槛。
    管理员不受限（主持人不参赛，但需要能查看与代操作）。
    """
    if user.role == ROLE_ADMIN:
        return user
    if user.house_id is None:
        raise AppError(
            403, ErrorCode.HOUSE_REQUIRED,
            "你的账号还没有归属学院，无法参赛。请联系管理员为你分配学院。",
            {"needs_house": True},
        )
    return user


async def require_login(user: User = Depends(get_current_user)) -> User:
    """竞赛相关接口的登录门槛。

    与直接依赖 `get_current_user` 的区别：把错误码统一成 `LOGIN_REQUIRED`。
    为什么值得单独一个码：前端据此区分「这个动作需要登录」与「会话过期了要重登」——
    前者应引导去登录页，后者应提示"登录已失效"。混用同一个码会让前端只能给出笼统提示。
    """
    return user
