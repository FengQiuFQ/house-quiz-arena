"""匿名访客身份（B2）。

学校场景的硬约束：**学生不注册也要能练习、能上榜**，但"匿名"必须满足三条：
  1. 上榜展示名固定为「枫禾学子」，多条记录用稳定序号区分（枫禾学子 #07）；
  2. 序号绑定一颗只存散列的随机令牌，**不含任何个人信息**（不是 IP、不是指纹、不是学号）；
  3. 服务端按令牌限流，避免一条脚本刷满榜单。

过期不续的做法：令牌有效期 30 天，到期后重新签发新的访客序号。
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import new_id, token_hash
from app.db.base import as_utc, utcnow
from app.db.models import GuestIdentity, User

GUEST_COOKIE = "mgus_guest"
GUEST_TTL_DAYS = 30
# 上榜默认展示名（匿名与登录用户都默认用它；登录用户可自愿改成昵称）
ANONYMOUS_NAME = "枫禾学子"


def display_name_for(guest: GuestIdentity | None) -> str:
    """匿名展示名。无访客身份时也返回默认名，绝不留空、绝不回落到真实姓名。"""
    if guest is None or not guest.display_seq:
        return ANONYMOUS_NAME
    return f"{ANONYMOUS_NAME} #{guest.display_seq:02d}"


async def resolve_guest(request: Request, db: AsyncSession) -> GuestIdentity | None:
    """按 Cookie 令牌查访客身份；无令牌或已失效返回 None（不新建）。"""
    raw = request.cookies.get(GUEST_COOKIE)
    if not raw:
        return None
    g = (await db.execute(select(GuestIdentity).where(
        GuestIdentity.token_hash == token_hash(raw)))).scalar_one_or_none()
    if g is None or not g.display_seq:
        # 无 display_seq = 身份初始化未完成（并发首访的中间态），视为新访客
        return None
    return g


async def ensure_guest(request: Request, response: Response,
                       db: AsyncSession) -> GuestIdentity:
    """取现有访客身份；没有就签发一个新的（并下发 Cookie）。

    并发首访可能同时创建两颗令牌——现象是同一浏览器出现两个序号，
    不影响正确性（各自独立会话），因此不为此引入锁。
    """
    g = await resolve_guest(request, db)
    if g is not None:
        g.last_seen_at = utcnow()
        return g

    token = secrets.token_urlsafe(24)
    seq = ((await db.execute(select(func.max(GuestIdentity.display_seq)))).scalar_one() or 0) + 1
    g = GuestIdentity(id=new_id(), token_hash=token_hash(token), display_seq=seq,
                      created_at=utcnow(), last_seen_at=utcnow())
    db.add(g)
    await db.flush()
    response.set_cookie(
        key=GUEST_COOKIE, value=token, httponly=True,
        secure=False if not request.url.scheme == "https" else True,
        samesite="lax", max_age=GUEST_TTL_DAYS * 86400, path="/",
    )
    return g


def guest_is_expired(g: GuestIdentity) -> bool:
    return as_utc(g.created_at) + timedelta(days=GUEST_TTL_DAYS) < as_utc(utcnow())


def learner_label(user: User | None, guest: GuestIdentity | None) -> str:
    """统一对外称呼：登录用户默认同样匿名（沿用 D7：不显示真实姓名）。"""
    if user is not None:
        return user.display_name or ANONYMOUS_NAME
    return display_name_for(guest)
