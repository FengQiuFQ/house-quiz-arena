"""账号、登录会话、换机与控制权（§1、§16）。

- 单账号单活跃会话：比赛中第二台设备必须凭原设备签发的一次性交接码登录，
  或由管理员（主持人）核验后吊销旧会话；非比赛时可用 force 重新验证接管。
- 换机原子递增 session generation；旧代次令牌在 get_current_user 即被拒绝。
"""
import time
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import (
    clear_csrf_cookie,
    clear_session_cookie,
    ensure_csrf_cookie,
    get_current_user,
    get_db,
    require_admin,
    set_session_cookie,
)
from app.core.errors import AppError, ErrorCode
from app.core.ratelimit import (
    assert_login_allowed,
    check_or_429,
    clear_login_failures,
    client_ip,
    note_login_failure,
)
from app.core.security import (
    hash_password,
    new_id,
    new_session_token,
    new_transfer_code,
    token_hash,
    verify_password,
)
from app.db.base import as_utc, utcnow
from app.db.models import (
    ControlLease,
    House,
    Match,
    MatchEligibility,
    MatchEntry,
    RoundInstance,
    MATCH_PAUSED,
    MATCH_RUNNING,
    LEGACY_ROLE_ALIASES,
    normalize_role,
    ROLE_ADMIN,
    ROLE_ADVISOR,
    ROLE_STUDENT,
    ROLES,
    ROUND_DISPLAYING,
    ROUND_OPEN,
    User,
    UserSession,
)
from app.modules.audit.service import audit

router = APIRouter(prefix="/api/v1")

# 一次性交接码（进程内，重启失效；重启后走管理员核验换机路径）
_transfer_codes: dict[str, tuple[str, float]] = {}


def _house_out(h: House | None) -> dict | None:
    if h is None:
        return None
    return {"key": h.key, "name_cn": h.name_cn, "name_en": h.name_en,
            "color_light": h.color_light, "color_deep": h.color_deep}


def user_out(db_user: User, house: House | None) -> dict:
    # 历史角色值（entrant）对外一律折算成现行角色，前端只需要认识三个角色
    role = normalize_role(db_user.role) or db_user.role
    return {
        "id": db_user.id, "username": db_user.username, "role": role,
        "display_name": db_user.display_name,
        "must_change_password": db_user.must_change_password,
        # 学生端据此显示"学院归属已确定，不能自行更改"；管理端据此区分来源
        "self_registered": bool(getattr(db_user, "self_registered", False)),
        "house_locked": bool(getattr(db_user, "house_locked", False)),
        "status": db_user.status,
        "house": _house_out(house),
    }


async def _get_house(db: AsyncSession, house_id: str | None) -> House | None:
    return await db.get(House, house_id) if house_id else None


def _purge_transfer_codes() -> None:
    now = time.monotonic()
    for k in [k for k, (_, exp) in _transfer_codes.items() if exp < now]:
        _transfer_codes.pop(k, None)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    force: bool = False
    transfer_code: str | None = Field(default=None, max_length=16)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response,
                db: AsyncSession = Depends(get_db)) -> dict:
    s = get_settings()
    # 登录限流（B1-E6）：失败计数 + 账号率 + 出口 IP 宽松率；此前该配置项从未被调用过。
    assert_login_allowed(body.username, request)
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if user is None:
        note_login_failure(body.username)
        raise AppError(401, ErrorCode.UNAUTHORIZED, "用户名或密码错误。")
    if not verify_password(body.password, user.password_hash):
        note_login_failure(body.username)
        await audit(db, None, "auth.login_failed", "user", user.id, {"username": body.username})
        await db.commit()
        raise AppError(401, ErrorCode.UNAUTHORIZED, "用户名或密码错误。")
    if user.status == "pending":
        # 待审核账号：明确告诉学生"不是密码错，是在等审核"，并给出补救路径
        raise AppError(403, ErrorCode.ACCOUNT_PENDING,
                       "账号正在等待管理员审核，通过后即可登录。如有急用请联系老师。")
    if user.status != "active":
        raise AppError(403, ErrorCode.ACCOUNT_DISABLED, "账号已停用，请联系主持人。")
    clear_login_failures(f"loginfail:{body.username}")

    # 交接码路径：比赛中换机（§16.2）
    _purge_transfer_codes()
    via_transfer = False
    if body.transfer_code:
        entry = _transfer_codes.pop(body.transfer_code, None)
        if entry is None or entry[0] != user.id:
            raise AppError(400, ErrorCode.TRANSFER_CODE_INVALID, "交接码无效或已过期。")
        via_transfer = True

    old_session = None
    if user.active_session_id:
        old_session = await db.get(UserSession, user.active_session_id)
        old_live = old_session is not None and old_session.revoked_at is None and \
            as_utc(old_session.expires_at) > as_utc(utcnow())
        if old_live and not via_transfer:
            # 是否必须交接，**由服务端按实际状态判定**，不再要求前端先带 force：
            # 只有真正"正在作答且原会话仍活跃"才要求交接码（见 _session_must_hand_over）。
            must_hand_over, why = await _session_must_hand_over(db, user, old_session)
            if must_hand_over:
                raise AppError(
                    409, ErrorCode.SESSION_ACTIVE,
                    "该账号的另一台设备正在答题。请在原设备生成一次性交接码后在此输入，"
                    "或联系主持人核验后由管理员吊销旧会话。",
                    details={"in_match": True, "needs_transfer": True, "reason": why})
            # 放行：旧会话标记为被取代（generation 会 +1，旧代次令牌立即失效）
            await audit(db, user.id, "auth.session_released", "user", user.id,
                        {"reason": why, "old_session": old_session.id if old_session else None})
        if old_session is not None and old_session.revoked_at is None:
            old_session.revoked_at = utcnow()

    # 会话代次 = 该账号最近一条会话（无论是否吊销）的代次 + 1（换机原子递增，§16.2）
    latest = (await db.execute(
        select(UserSession).where(UserSession.user_id == user.id)
        .order_by(UserSession.created_at.desc()).limit(1))).scalar_one_or_none()
    generation = (latest.generation + 1) if latest is not None else 1
    token = new_session_token()
    from datetime import timedelta
    expires = (as_utc(utcnow()) + timedelta(hours=s.session_ttl_hours)).replace(tzinfo=None)
    sess = UserSession(
        id=new_id(), user_id=user.id, token_hash=token_hash(token), generation=generation,
        created_at=utcnow(), expires_at=expires, last_seen_at=utcnow(),
    )
    db.add(sess)
    user.active_session_id = sess.id
    # 换约会话时必须**一并清掉旧的控制权租约**。
    # 否则：新会话的 id 与旧租约里的 session_id 不一致，claim 会一直返回
    # 409「控制权由其他会话持有」，该账号**永远拿不到答题控制权、永远交不了答案**
    # （本项目实测踩过：库里租约指向已撤销的旧会话，新登录后每次 claim 都 409）。
    stale_lease = await db.get(ControlLease, user.id)
    if stale_lease is not None:
        await db.delete(stale_lease)
    await audit(db, user.id, "auth.login", "user", user.id,
                {"generation": generation, "via_transfer": via_transfer, "force": body.force,
                 "cleared_stale_lease": stale_lease is not None})
    await db.commit()

    set_session_cookie(response, token)
    ensure_csrf_cookie(response, token)   # 写操作需带 X-CSRF-Token（B1-E5）
    house = await _get_house(db, user.house_id)
    return {"user": user_out(user, house), "session_generation": generation,
            "csrf_token": token}


async def _user_in_live_match(db: AsyncSession, user_id: str) -> bool:
    stmt = (
        select(Match.id)
        .join(MatchEntry, MatchEntry.match_id == Match.id)
        .where(MatchEntry.user_id == user_id, Match.status.in_(["lobby", "running", "paused"]))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _session_must_hand_over(db: AsyncSession, user: User,
                                  old_session: UserSession | None) -> tuple[bool, str]:
    """换机时**是否必须**走交接码/主持人核验。返回 (是否需要, 原因)。

    单账号单活跃会话的目的只有一个：**防止同一名选手在两台设备上同时作答**
    （一人两屏会破坏抢答的公平性）。因此"要不要交接"必须精确针对这件事，
    不能把任何"有比赛"的情形都当成需要交接。

    三条门槛，缺一不可：

    ① **管理员从不参与答题**：主持人换机是日常工作（后台、投屏、结果页来回切），
       让他去找"原设备生成交接码"是荒谬的。比赛由选手参加，不由管理员参加。

    ② **必须真的在作答界面**：`running / paused`（比赛进行中）才可能出现作答界面。
       `lobby`（签到中）只是在大厅等着，此时换设备毫无公平性问题 ——
       原来把 lobby 也算"比赛中"，于是"签到完就再也换不了设备了"。

    ③ **原会话必须是近期活跃的**：60 秒内没有任何请求，就认为那个标签页已经不用了
       （关掉、切走、断网、手机锁屏）。此时直接放行新登录，而不是逼人去掏交接码。
       这就是产品要求的那句"超过 1 分钟没有答题，活跃就可以登"。
       依据是 `sessions.last_seen_at`，每次请求都会刷新（见 core/deps.py）。
    """
    if user.role == "admin":
        return False, "admin"

    if old_session is None or old_session.last_seen_at is None:
        return False, "no_seen"

    idle_s = (as_utc(utcnow()) - as_utc(old_session.last_seen_at)).total_seconds()
    if idle_s > get_settings().handover_idle_seconds:
        return False, f"idle_{int(idle_s)}s"

    # 比赛必须处于"可能正在作答"的阶段
    match = (await db.execute(
        select(Match)
        .join(MatchEntry, MatchEntry.match_id == Match.id)
        .where(MatchEntry.user_id == user.id,
               Match.status.in_([MATCH_RUNNING, MATCH_PAUSED]))
        .limit(1)
    )).scalar_one_or_none()
    if match is None:
        return False, "not_answering"

    # 而且**确实有一道题正在等他答**（读题或作答阶段）。
    # 只是"在名单里"不算：题间过渡、等下一题、已交卷之后都不影响换设备。
    open_round = (await db.execute(
        select(RoundInstance.id)
        .where(RoundInstance.match_id == match.id,
               RoundInstance.status.in_([ROUND_DISPLAYING, ROUND_OPEN]))
        .limit(1)
    )).scalar_one_or_none()
    if open_round is None:
        return False, "between_rounds"

    return True, "answering"


@router.post("/auth/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    token = request.cookies.get(get_settings().cookie_name)
    if token:
        sess = (await db.execute(
            select(UserSession).where(UserSession.token_hash == token_hash(token))
        )).scalar_one_or_none()
        if sess is not None and sess.revoked_at is None:
            sess.revoked_at = utcnow()
            user = await db.get(User, sess.user_id)
            if user is not None and user.active_session_id == sess.id:
                user.active_session_id = None
            lease = await db.get(ControlLease, sess.user_id)
            if lease is not None and lease.session_id == sess.id:
                await db.delete(lease)
            await db.commit()
    clear_session_cookie(response)
    clear_csrf_cookie(response)
    return {"ok": True}


@router.get("/auth/me")
async def me(request: Request, user: User = Depends(get_current_user),
             db: AsyncSession = Depends(get_db)) -> dict:
    house = await _get_house(db, user.house_id)
    out: dict[str, Any] = user_out(user, house)
    out["session_generation"] = request.state.session.generation
    part = (await db.execute(
        select(MatchEntry, Match).join(Match, Match.id == MatchEntry.match_id)
        .where(MatchEntry.user_id == user.id, Match.status.in_(["lobby", "running", "paused"]))
        .limit(1)
    )).first()
    if part is not None:
        entry, match = part
        out["active_participation"] = {"match_id": match.id, "title": match.title,
                                       "status": match.status, "is_captain": entry.is_captain,
                                       "display_alias": entry.display_alias}
    return out


@router.post("/auth/transfer")
async def transfer(request: Request, user: User = Depends(get_current_user),
                   db: AsyncSession = Depends(get_db)) -> dict:
    """原设备确认交接：立即吊销本会话并签发一次性交接码（§16.2）。"""
    sess = request.state.session
    if sess.revoked_at is None:
        sess.revoked_at = utcnow()
    if user.active_session_id == sess.id:
        user.active_session_id = None
    code = new_transfer_code()
    _transfer_codes[code] = (user.id, time.monotonic() + get_settings().transfer_code_ttl_minutes * 60)
    lease = await db.get(ControlLease, user.id)
    if lease is not None and lease.session_id == sess.id:
        await db.delete(lease)
    await audit(db, user.id, "auth.transfer", "user", user.id, {})
    await db.commit()
    return {"transfer_code": code,
            "expires_in_minutes": get_settings().transfer_code_ttl_minutes}


class ClaimOut(BaseModel):
    control_token: str
    session_id: str


@router.post("/auth/control/claim")
async def claim_control(request: Request, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)) -> dict:
    """签发答题控制权租约（§16.2）。

    服务端保证：同一账号只有一个租约，且只能由当前活跃会话刷新——
    其他会话（旧设备）无法夺取；同会话多标签页的独占由前端 BroadcastChannel 协调，仅为体验优化。

    **兜底自动回收**：若租约指向的会话既不是当前会话、也不是该用户当前的活跃会话，
    说明它是残留（例如换机流程被中断、或旧版本未清理），此时直接回收并写审计，
    而不是把用户永久锁在"控制权被他人持有"的状态里 —— 那会让学生在现场彻底交不了答案。
    """
    lease = await db.get(ControlLease, user.id)
    if lease is not None and lease.session_id != request.state.session_id:
        is_stale = lease.session_id != user.active_session_id
        if not is_stale:
            # 租约属于**当前另一台活跃设备**：这是正常的多设备争夺，拒绝并要求交接
            raise AppError(409, ErrorCode.CONTROL_TOKEN_INVALID,
                           "控制权由其他设备持有，请先完成换机交接。")
        await audit(db, user.id, "auth.control_reclaim", "user", user.id,
                    {"stale_session": lease.session_id, "current": request.state.session_id})
        await db.delete(lease)
        await db.flush()
        lease = None

    token = new_session_token()
    # 先把主键取到本地变量：下面并发分支里的 `db.rollback()` 会让 ORM 实例**过期**，
    # 之后任何属性访问都会尝试重新加载 —— 在 async 会话里那会直接抛 MissingGreenlet（500）。
    # 这条路径恰恰是学生连点两下答题页时必然走到的分支，实测表现为"页面显示服务器内部错误"。
    user_id = user.id
    if lease is None:
        lease = ControlLease(user_id=user_id, session_id=request.state.session_id, token=token_hash(token))
        db.add(lease)
        try:
            await db.flush()
        except IntegrityError:
            # 并发竞态：同一账号两个请求同时判定"没有租约"，双双 INSERT，
            # 后到的那条撞 `UNIQUE(user_id)`。这不是异常情况 —— 学生在答题页
            # 连点两下就会触发（前台两个请求几乎同时到达）。
            # 处理方式：回滚这次插入，改为**更新**已存在的那一行（后到者胜，
            # 与"同一账号只有一份租约"的语义一致）。
            await db.rollback()
            lease = await db.get(ControlLease, user_id)
            if lease is None:
                raise AppError(409, ErrorCode.CONTROL_TOKEN_INVALID,
                               "控制权认领冲突，请重试。")
            lease.token = token_hash(token)
            lease.session_id = request.state.session_id
            lease.updated_at = utcnow()
    else:
        lease.token = token_hash(token)
        lease.updated_at = utcnow()
    await db.commit()
    return {"control_token": token, "session_id": request.state.session_id}


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/auth/change-password")
async def change_password(body: ChangePasswordIn, request: Request, response: Response,
                          user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)) -> dict:
    """修改口令。成功后**轮换会话令牌**（B1-E7）：

    CSRF 令牌＝会话明文，因此改密后旧令牌必须立即失效——否则旧设备上残留的页面
    仍能凭旧 Cookie 继续操作。轮换 = 吊销当前会话 + 签发新会话 + 重下两枚 Cookie。
    """
    if not verify_password(body.old_password, user.password_hash):
        raise AppError(400, ErrorCode.UNAUTHORIZED, "原密码错误。")
    if body.new_password == body.old_password:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "新密码不能与原密码相同。")

    old = request.state.session
    old.revoked_at = utcnow()

    s = get_settings()
    from datetime import timedelta
    token = new_session_token()
    expires = (as_utc(utcnow()) + timedelta(hours=s.session_ttl_hours)).replace(tzinfo=None)
    sess = UserSession(id=new_id(), user_id=user.id, token_hash=token_hash(token),
                       generation=old.generation + 1, created_at=utcnow(),
                       expires_at=expires, last_seen_at=utcnow())
    db.add(sess)
    user.active_session_id = sess.id
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    await audit(db, user.id, "auth.change_password", "user", user.id,
                {"generation": sess.generation})
    await db.commit()

    set_session_cookie(response, token)
    ensure_csrf_cookie(response, token)
    return {"ok": True, "session_generation": sess.generation, "csrf_token": token}


# ── 自助注册（House 自选，注册后自己不能改）──────────────────────────────────

class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    password: str = Field(min_length=8, max_length=128)
    # 学生自己选 House。**必填**：不选就没法算学院分，也避免事后被追问"你哪个院"。
    house_key: str = Field(min_length=2, max_length=32)
    display_name: str | None = Field(default=None, max_length=32)
    invite_code: str | None = Field(default=None, max_length=64)


@router.get("/auth/registration")
async def registration_info(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """注册页需要的信息：是否开放、可选 House、是否需要邀请码。

    公开接口（未登录可读）。**不下发邀请码本身**，只告诉是否需要。
    """
    from app.modules import settings as rts
    open_ = bool(await rts.service.get(db, rts.service.KEY_REGISTRATION_OPEN))
    needs_code = bool(await rts.service.get(db, rts.service.KEY_REGISTRATION_CODE))
    houses = (await db.execute(
        select(House).where(House.is_active.is_(True)).order_by(House.created_at)
    )).scalars().all()
    return {
        "open": open_,
        "needs_invite_code": needs_code,
        # 前端据此在注册页提前说明"提交后需要等审核"，而不是注册完才发现登不进去
        "needs_review": bool(await rts.service.get(db, rts.service.KEY_REGISTRATION_REVIEW)),
        "houses": [{"key": h.key, "name_cn": h.name_cn, "name_en": h.name_en,
                    "color_light": h.color_light, "color_deep": h.color_deep} for h in houses],
    }


@router.post("/auth/register")
async def register(body: RegisterIn, request: Request, response: Response,
                   db: AsyncSession = Depends(get_db)) -> dict:
    """学生自助注册。

    规则（与产品约定一致）：
      · 是否可以注册由后台开关决定；关闭时一律拒绝，并给出中性提示。
      · House **必选且注册后自己不能改**：前端不提供修改入口，后端也没有对应通道；
        只有管理员能经 `/admin/users/{id}` 调整。
      · 默认角色由后台设置（通常 student：可练习、可上榜，参加正式赛需管理员加资格）。
      · 注册即登录（下发会话与 CSRF Cookie），避免学生注册完还要再登一次。
    """
    from app.modules import settings as rts
    cfg = await rts.service.get_all(db)
    if not cfg[rts.service.KEY_REGISTRATION_OPEN]:
        raise AppError(403, ErrorCode.FORBIDDEN,
                       "当前未开放自助注册。请联系老师或管理员开通账号。")
    need_code = str(cfg[rts.service.KEY_REGISTRATION_CODE] or "")
    if need_code and (body.invite_code or "").strip() != need_code:
        raise AppError(403, ErrorCode.FORBIDDEN, "邀请码不正确。")

    check_or_429(f"register:{client_ip(request)}", 30)
    exists = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if exists is not None:
        raise AppError(409, ErrorCode.USERNAME_TAKEN, "该用户名已被使用，换一个试试。")

    house = (await db.execute(
        select(House).where(House.key == body.house_key, House.is_active.is_(True))
    )).scalar_one_or_none()
    if house is None:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "请选择一个有效的学院。")

    default_role = normalize_role(str(cfg[rts.service.KEY_REGISTRATION_DEFAULT_ROLE] or "")) or "student"
    if default_role not in ROLES:
        default_role = "student"

    # 是否需要管理员审核。两档的实际差别：
    #   免审 → status=active，注册即登录，选完学院马上能用；
    #   需审 → status=pending，**不能登录**（登录被拒并提示等待审核），
    #          管理员在后台通过后才变 active。注册本身仍会成功并给出明确说明，
    #          否则学生填完表单看到报错会以为白填了。
    need_review = bool(cfg[rts.service.KEY_REGISTRATION_REVIEW])
    initial_status = "pending" if need_review else "active"

    u = User(id=new_id(), username=body.username,
             password_hash=hash_password(body.password),
             role=default_role, house_id=house.id,
             display_name=(body.display_name or None),
             status=initial_status, must_change_password=False,
             self_registered=True,
             # 学生自选的 House 立即锁定：注册后本人不可更改（管理员仍可调整）
             house_locked=True,
             created_at=utcnow())
    db.add(u)
    await db.flush()

    if need_review:
        # 待审核不下发会话：否则等于"注册即可用"，审核就形同虚设
        await audit(db, None, "auth.register_pending", "user", u.id,
                    {"username": u.username, "house": house.key, "role": default_role})
        await db.commit()
        return {
            "pending_review": True,
            "user": user_out(u, house),
            "note": "注册已提交，等待管理员审核。审核通过后即可用该账号登录。",
        }

    # 免审：注册即登录，学生不用再登一次
    s = get_settings()
    from datetime import timedelta
    token = new_session_token()
    expires = (as_utc(utcnow()) + timedelta(hours=s.session_ttl_hours)).replace(tzinfo=None)
    sess = UserSession(id=new_id(), user_id=u.id, token_hash=token_hash(token), generation=1,
                       created_at=utcnow(), expires_at=expires, last_seen_at=utcnow())
    db.add(sess)
    u.active_session_id = sess.id
    u.last_login_at = utcnow()
    await audit(db, None, "auth.register", "user", u.id,
                {"username": u.username, "house": house.key, "role": default_role,
                 "self_registered": True})
    await db.commit()

    set_session_cookie(response, token)
    ensure_csrf_cookie(response, token)
    return {"pending_review": False, "user": user_out(u, house), "csrf_token": token,
            "note": "学院归属已按你的选择确定，注册后不能自行更改；如需调整请联系管理员。"}


# ── 管理端：账号与资格 ────────────────────────────────────────────────────────


class UserCreateIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role: str = "student"
    house_key: str | None = None
    display_name: str | None = Field(default=None, max_length=64)
    must_change_password: bool = True


@router.get("/admin/users")
async def list_users(role: str | None = None, q: str | None = None, page: int = 1, size: int = 50,
                     _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    stmt = select(User).order_by(User.username)
    if role:
        # 过滤条件也折算：`?role=entrant` 是历史写法，语义上就是"学生"。
        # 同时把库里可能还残留的旧值一起匹配，避免"筛选学生"漏掉未迁移的账号。
        want = normalize_role(role) or role
        legacy = [old for old, new in LEGACY_ROLE_ALIASES.items() if new == want]
        stmt = stmt.where(User.role.in_([want, *legacy]))
    if q:
        stmt = stmt.where(User.username.contains(q))
    rows = (await db.execute(stmt.limit(size).offset((page - 1) * size))).scalars().all()
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}
    return {"items": [user_out(u, houses.get(u.house_id)) for u in rows], "page": page, "size": size}


@router.post("/admin/users")
async def create_user(body: UserCreateIn, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    role = normalize_role(body.role) or body.role
    if role not in ROLES:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, f"角色必须是 {ROLES}。")
    exists = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if exists is not None:
        raise AppError(409, ErrorCode.USERNAME_TAKEN, "用户名已存在。")
    house = None
    if body.house_key:
        house = (await db.execute(select(House).where(House.key == body.house_key))).scalar_one_or_none()
        if house is None:
            raise AppError(404, ErrorCode.NOT_FOUND, "House 不存在。")
    # 只有"智囊"必须绑定学院（求援按学院发放）。学生**不强制**：
    # 没有学院也能练习与上榜，等他要参赛时再补 —— 否则建号就先卡一次。
    if role == ROLE_ADVISOR and house is None:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "智囊账号必须绑定学院，求援按学院发放。")
    u = User(id=new_id(), username=body.username, password_hash=hash_password(body.password),
             role=role, house_id=house.id if house else None,
             display_name=body.display_name, must_change_password=body.must_change_password)
    db.add(u)
    await db.flush()
    await audit(db, admin.id, "admin.create_user", "user", u.id, {"role": role})
    await db.commit()
    return user_out(u, house)


class UserPatchIn(BaseModel):
    role: str | None = None
    house_key: str | None = None
    status: str | None = None
    display_name: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


@router.patch("/admin/users/{user_id}")
async def patch_user(user_id: str, body: UserPatchIn, admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_db)) -> dict:
    u = await db.get(User, user_id)
    if u is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "用户不存在。")
    changes: dict[str, Any] = {}
    new_role = normalize_role(body.role)
    if new_role and new_role in ROLES and new_role != u.role:
        u.role = new_role
        changes["role"] = new_role
    if body.house_key is not None:
        h = (await db.execute(select(House).where(House.key == body.house_key))).scalar_one_or_none()
        if h is None:
            raise AppError(404, ErrorCode.NOT_FOUND, "House 不存在。")
        u.house_id = h.id
        changes["house_key"] = body.house_key  # 开赛后以冻结名单快照为准，历史不受影响（§10.3）
    if body.status and body.status != u.status:
        u.status = body.status
        changes["status"] = body.status
    if body.display_name is not None:
        u.display_name = body.display_name
        changes["display_name"] = True
    if body.password:
        u.password_hash = hash_password(body.password)
        u.must_change_password = True
        changes["password_reset"] = True
    await audit(db, admin.id, "admin.patch_user", "user", u.id, changes)
    await db.commit()
    return user_out(u, await _get_house(db, u.house_id))


# ── 管理端：House 归属 / 封禁 / 数据大屏 / 注册开关 ──────────────────────────

class HouseAssignIn(BaseModel):
    house_key: str | None = Field(default=None, max_length=32)
    reason: str | None = Field(default=None, max_length=200)


@router.post("/admin/users/{user_id}/house")
async def assign_house(user_id: str, body: HouseAssignIn, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)) -> dict:
    """管理员调整 House 归属 —— **唯一**能改归属的通道。

    学生注册时自选并锁定，本人无任何通路修改（前端无入口、后端无接口）。
    开赛后以冻结名单快照为准，因此调整归属不会改动历史成绩（§10.3）。
    house_key=null 表示解除绑定（例如转学/退赛）。
    """
    u = await db.get(User, user_id)
    if u is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "用户不存在。")
    before = await _get_house(db, u.house_id)
    if body.house_key is None:
        u.house_id = None
        u.house_locked = False
    else:
        h = (await db.execute(
            select(House).where(House.key == body.house_key, House.is_active.is_(True))
        )).scalar_one_or_none()
        if h is None:
            raise AppError(404, ErrorCode.NOT_FOUND, "House 不存在。")
        u.house_id = h.id
        u.house_locked = True
    await audit(db, admin.id, "admin.assign_house", "user", u.id,
                {"from": before.key if before else None, "to": body.house_key,
                 "reason": body.reason, "was_self_registered": bool(u.self_registered)})
    await db.commit()
    return {"ok": True, "user": user_out(u, await _get_house(db, u.house_id))}


class BanIn(BaseModel):
    banned: bool
    reason: str | None = Field(default=None, max_length=200)


@router.post("/admin/users/{user_id}/ban")
async def ban_user(user_id: str, body: BanIn, admin: User = Depends(require_admin),
                   db: AsyncSession = Depends(get_db)) -> dict:
    """封禁 / 解封账号。

    封禁同时做三件事，只改 status 是不够的：
      1. status=disabled —— 之后登录被拒；
      2. 吊销当前会话与答题控制权 —— **立即**踢下线，否则已登录的设备还能继续答题；
      3. 记录原因与时间，写审计。
    """
    u = await db.get(User, user_id)
    if u is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "用户不存在。")
    if u.role == ROLE_ADMIN and body.banned:
        raise AppError(422, ErrorCode.VALIDATION_ERROR, "不能封禁管理员账号。")

    if body.banned:
        u.status = "disabled"
        u.disabled_reason = body.reason or "管理员封禁"
        u.disabled_at = utcnow()
        if u.active_session_id:
            sess = await db.get(UserSession, u.active_session_id)
            if sess is not None and sess.revoked_at is None:
                sess.revoked_at = utcnow()
        u.active_session_id = None
        lease = await db.get(ControlLease, user_id)
        if lease is not None:
            await db.delete(lease)
    else:
        u.status = "active"
        u.disabled_reason = None
        u.disabled_at = None
    await audit(db, admin.id, "admin.ban" if body.banned else "admin.unban",
                "user", u.id, {"reason": body.reason})
    await db.commit()
    return {"ok": True, "status": u.status}


@router.get("/admin/overview")
async def admin_overview(_admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    """后台数据大屏：各 House 成员数、账号构成、封禁情况、注册开关状态。

    口径说明（页面必须一起显示，否则"人数"会被误读）：
      · members          已绑定该 House 的账号数（含被封禁）
      · active_members   状态为 active 的成员数 —— **这才是能上场的人数**
      · banned           被封禁的成员数
      · students / advisors  按角色分（历史角色 entrant 计入 students）

    这里不再有"参赛"一栏：角色不再决定能不能上场，任何绑定了学院的账号都能代表本院参赛，
    所以"能上场的人数"就是 active_members 本身。
    """
    houses = (await db.execute(select(House).order_by(House.created_at))).scalars().all()
    users = (await db.execute(select(User))).scalars().all()

    per_house = {h.id: {"house_key": h.key, "house_name": h.name_cn,
                        "color_light": h.color_light, "color_deep": h.color_deep,
                        "members": 0, "active_members": 0, "banned": 0,
                        "students": 0, "advisors": 0, "admins": 0} for h in houses}
    unassigned = {"members": 0, "active_members": 0, "banned": 0}
    by_role: dict[str, int] = {}
    banned_total = 0

    for u in users:
        role = normalize_role(u.role) or u.role          # 旧值 entrant → student
        by_role[role] = by_role.get(role, 0) + 1
        is_banned = u.status != "active"
        if is_banned:
            banned_total += 1
        row = per_house.get(u.house_id)
        if row is None:
            unassigned["members"] += 1
            if not is_banned:
                unassigned["active_members"] += 1
            else:
                unassigned["banned"] += 1
            continue
        row["members"] += 1
        if is_banned:
            row["banned"] += 1
        else:
            row["active_members"] += 1
        if role == ROLE_STUDENT:
            row["students"] += 1
        elif role == ROLE_ADVISOR:
            row["advisors"] += 1
        elif role == ROLE_ADMIN:
            row["admins"] += 1

    from app.modules import settings as rts
    cfg = await rts.service.get_all(db)

    self_reg = sum(1 for u in users if getattr(u, "self_registered", False))
    # 人数均衡提示：最高与最低之差超过 20% 时提醒，避免"某院没人"才发现
    counts = [r["active_members"] for r in per_house.values()]
    imbalance = None
    if counts and max(counts) > 0:
        spread = (max(counts) - min(counts)) / max(counts)
        if spread > 0.2:
            imbalance = {
                "spread_pct": round(spread * 100, 1),
                "max": max(counts), "min": min(counts),
                "hint": "各学院可上场人数差距较大。若采用人均平均分制，人数少的一方每个人权重更大；"
                        "如需均衡，可在赛前调整归属或改用固定席位赛。",
            }

    return {
        "houses": list(per_house.values()),
        "unassigned": unassigned,
        "totals": {"users": len(users), "banned": banned_total,
                   "self_registered": self_reg, "by_role": by_role},
        "settings": cfg,
        "imbalance": imbalance,
    }


class SettingsIn(BaseModel):
    registration_open: bool | None = None
    registration_code: str | None = Field(default=None, max_length=64)
    registration_default_role: str | None = Field(default=None, max_length=16)
    registration_review: bool | None = None


@router.get("/admin/settings")
async def get_settings_api(_admin: User = Depends(require_admin),
                           db: AsyncSession = Depends(get_db)) -> dict:
    from app.modules import settings as rts
    return await rts.service.get_all(db)


@router.patch("/admin/settings")
async def patch_settings(body: SettingsIn, admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    """修改运行期开关（立即生效并写审计）。"""
    from app.modules import settings as rts
    changes: dict[str, object] = {}
    if body.registration_open is not None:
        await rts.service.set_value(db, rts.service.KEY_REGISTRATION_OPEN,
                                    body.registration_open, admin.id)
        changes["registration_open"] = body.registration_open
    if body.registration_code is not None:
        await rts.service.set_value(db, rts.service.KEY_REGISTRATION_CODE,
                                    body.registration_code, admin.id)
        changes["registration_code_set"] = bool(body.registration_code)
    if body.registration_default_role is not None:
        role = normalize_role(body.registration_default_role) or body.registration_default_role
        if role not in ROLES:
            raise AppError(422, ErrorCode.VALIDATION_ERROR, f"角色必须是 {ROLES}。")
        await rts.service.set_value(db, rts.service.KEY_REGISTRATION_DEFAULT_ROLE,
                                    role, admin.id)
        changes["registration_default_role"] = role
    if body.registration_review is not None:
        await rts.service.set_value(db, rts.service.KEY_REGISTRATION_REVIEW,
                                    body.registration_review, admin.id)
        changes["registration_review"] = body.registration_review
    await audit(db, admin.id, "admin.settings", "settings", None, changes)
    await db.commit()
    return await rts.service.get_all(db)


class ReviewUserIn(BaseModel):
    approve: bool
    reason: str | None = Field(default=None, max_length=200)


@router.get("/admin/users/pending")
async def list_pending_users(_admin: User = Depends(require_admin),
                             db: AsyncSession = Depends(get_db)) -> dict:
    """待审核的自助注册账号。管理员在后台据此逐个通过或拒绝。"""
    rows = (await db.execute(
        select(User).where(User.status == "pending", User.self_registered.is_(True))
        .order_by(User.created_at)
    )).scalars().all()
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}
    return {"items": [
        {**user_out(u, houses.get(u.house_id)), "created_at": u.created_at}
        for u in rows
    ], "total": len(rows)}


@router.post("/admin/users/{user_id}/review")
async def review_user(user_id: str, body: ReviewUserIn, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """审核自助注册：通过 → active（可登录）；拒绝 → disabled（并记原因）。

    只允许对**待审核**账号操作：对已 active 的账号调这个接口通常是误操作，
    直接拒绝比默默改状态更安全（要封禁请用 /ban，语义与审计都更清楚）。
    """
    u = await db.get(User, user_id)
    if u is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "用户不存在。")
    if u.status != "pending":
        raise AppError(409, ErrorCode.VALIDATION_ERROR,
                       f"该账号当前状态是 {u.status}，不在待审核列表中。")
    if body.approve:
        u.status = "active"
        u.disabled_reason = None
        u.disabled_at = None
    else:
        u.status = "disabled"
        u.disabled_reason = body.reason or "注册未通过审核"
        u.disabled_at = utcnow()
    await audit(db, admin.id, "admin.review_user", "user", u.id,
                {"approve": body.approve, "reason": body.reason,
                 "house": (await _get_house(db, u.house_id)).key if u.house_id else None})
    await db.commit()
    return {"ok": True, "status": u.status, "user": user_out(u, await _get_house(db, u.house_id))}


@router.post("/admin/users/{user_id}/revoke-session")
async def revoke_session(user_id: str, admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    """主持人核验换机（§16.2）：吊销活跃会话并清除控制权租约。"""
    u = await db.get(User, user_id)
    if u is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "用户不存在。")
    if u.active_session_id:
        sess = await db.get(UserSession, u.active_session_id)
        if sess is not None and sess.revoked_at is None:
            sess.revoked_at = utcnow()
    u.active_session_id = None
    lease = await db.get(ControlLease, user_id)
    if lease is not None:
        await db.delete(lease)
    await audit(db, admin.id, "admin.revoke_session", "user", user_id, {})
    await db.commit()
    return {"ok": True}


class EligibilityIn(BaseModel):
    usernames: list[str] = Field(default_factory=list, max_length=500)


# ── 赛前预登记（可选）────────────────────────────────────────────────────────
# **这两条接口不再决定谁能参赛。**
# 现行规则是"按学院参赛"：账号绑定了学院，就能代表本院签到上场，不需要任何额外资格。
# 保留它们只是为了让现场的"我到时会来"能记一笔（方便提前估人数、打印名单），
# 以及让历史数据与既有脚本继续可用。响应里带回 notice 明说这件事，
# 免得后来人看到 "eligibilities" 这个词又把它当成一道门。
_ELIG_NOTICE = ("预登记仅用于赛前估人数与打印名单，不影响能否签到上场。"
                "参赛资格 = 账号已绑定本场范围内的学院。")


@router.post("/admin/matches/{match_id}/eligibilities")
async def add_eligibility(match_id: str, body: EligibilityIn, admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    match = await db.get(Match, match_id)
    if match is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "比赛不存在。")
    if match.status not in ("draft", "lobby"):
        raise AppError(409, ErrorCode.MATCH_NOT_JOINABLE, "名单已冻结，不能再修改预登记。")
    added, missing = [], []
    for name in body.usernames:
        u = (await db.execute(select(User).where(User.username == name))).scalar_one_or_none()
        if u is None:
            missing.append(name)
            continue
        dup = (await db.execute(select(MatchEligibility).where(
            MatchEligibility.match_id == match_id, MatchEligibility.user_id == u.id
        ))).scalar_one_or_none()
        if dup is None:
            db.add(MatchEligibility(id=new_id(), match_id=match_id, user_id=u.id,
                                    approved_by=admin.id))
            added.append(name)
    await audit(db, admin.id, "admin.add_eligibility", "match", match_id,
                {"added": added, "missing": missing})
    await db.commit()
    return {"added": added, "missing": missing, "notice": _ELIG_NOTICE}


@router.get("/admin/matches/{match_id}/eligibilities")
async def list_eligibility(match_id: str, admin: User = Depends(require_admin),
                           db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(
        select(MatchEligibility, User).join(User, User.id == MatchEligibility.user_id)
        .where(MatchEligibility.match_id == match_id)
    )).all()
    houses = {h.id: h for h in (await db.execute(select(House))).scalars()}
    return {"items": [{"username": u.username, "role": normalize_role(u.role) or u.role,
                       "house": _house_out(houses.get(u.house_id))} for _, u in rows],
            "notice": _ELIG_NOTICE}
