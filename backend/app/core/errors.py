"""统一业务错误：code / message / request_id / details（§14.1）。"""
from typing import Any


class ErrorCode:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    CSRF_FAILED = "CSRF_FAILED"
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    ACCOUNT_PENDING = "ACCOUNT_PENDING"        # 自助注册待管理员审核
    USERNAME_TAKEN = "USERNAME_TAKEN"
    SESSION_ACTIVE = "SESSION_ACTIVE"          # 已有活跃会话，比赛中需交接（§16.2）
    SESSION_REPLACED = "SESSION_REPLACED"      # 会话已被新设备取代（§14.1）
    TRANSFER_CODE_INVALID = "TRANSFER_CODE_INVALID"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"              # 无本场参赛资格
    HOUSE_REQUIRED = "HOUSE_REQUIRED"          # 账号未绑定 House，不能参赛
    NOT_YOUR_HOUSE = "NOT_YOUR_HOUSE"          # 结果公布前只能看自己学院的比赛
    LOGIN_REQUIRED = "LOGIN_REQUIRED"          # 竞赛相关操作必须登录
    MATCH_NOT_JOINABLE = "MATCH_NOT_JOINABLE"
    ROUND_NOT_OPEN = "ROUND_NOT_OPEN"
    ROUND_CLOSED = "ROUND_CLOSED"
    ANSWER_ALREADY_LOCKED = "ANSWER_ALREADY_LOCKED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"  # 同键不同内容
    INVALID_OPTION = "INVALID_OPTION"
    CONTROL_TOKEN_INVALID = "CONTROL_TOKEN_INVALID"
    STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    RESULT_NOT_PUBLIC = "RESULT_NOT_PUBLIC"
    AID_NOT_AVAILABLE = "AID_NOT_AVAILABLE"
    AID_WINDOW_CLOSED = "AID_WINDOW_CLOSED"
    PRACTICE_ERROR = "PRACTICE_ERROR"


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
