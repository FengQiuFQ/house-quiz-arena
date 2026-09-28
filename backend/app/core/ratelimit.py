"""进程内滑动窗口限流。

注意（§10.5）：限流以「账号 + 操作类型」为主，IP 只作宽松辅助——
学校大量用户共享出口 IP，不能把整所学校封住。首版单进程，Redis 引入后需迁移到共享存储。
"""
import time
from collections import defaultdict, deque

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

_buckets: dict[str, deque[float]] = defaultdict(deque)


def allow(key: str, limit: int, window_s: float = 60.0) -> bool:
    now = time.monotonic()
    q = _buckets[key]
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def check_or_429(key: str, limit: int, window_s: float = 60.0) -> None:
    if not allow(key, limit, window_s):
        raise AppError(429, ErrorCode.RATE_LIMITED, "操作过于频繁，请稍后重试。")


def client_ip(request) -> str:
    """宽松取客户端 IP：仅作辅助键，绝不作为主要限流依据（学校共用出口 IP）。"""
    if request is None:
        return "-"
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "-")[:45]


# ── 登录失败计数（B1-E6）────────────────────────────────────────────────────
# 与「成功率限流」分开：成功不消耗额度，失败累计，成功即清零。
# 这样正常用户偶尔打错一次不会把自己锁住，而爆破会被迅速掐断。

_fails: dict[str, deque[float]] = defaultdict(deque)


def fail_key(username: str) -> str:
    """失败计数键。**必须**所有读写都经此函数——曾经读 `loginfail:{u}` 而写裸 `u`，
    结果计数器永远是 0，限流看似接上实则完全失效。"""
    return f"loginfail:{username}"


def note_login_failure(username: str) -> None:
    _fails[fail_key(username)].append(time.monotonic())


def clear_login_failures(username: str) -> None:
    _fails.pop(fail_key(username), None)


def assert_login_allowed(username: str, request=None, window_s: float = 60.0) -> None:
    """三次检查：账号失败次数、账号总请求率、出口 IP 总请求率（宽松）。"""
    s = get_settings()
    now = time.monotonic()
    q = _fails[fail_key(username)]
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= s.rate_limit_login_fail_per_min:
        wait = int(window_s - (now - q[0])) + 1
        raise AppError(429, ErrorCode.RATE_LIMITED,
                       f"该账号连续登录失败次数过多，请 {wait} 秒后重试，或联系主持人。",
                       {"retry_after_s": wait})
    check_or_429(f"login_acct:{username}", s.rate_limit_login_per_min, window_s)
    check_or_429(f"login_ip:{client_ip(request)}", s.rate_limit_login_ip_per_min, window_s)
