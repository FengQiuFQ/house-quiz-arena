"""口令散列、令牌与标识生成。OWASP 建议：bcrypt/argon2，令牌只存散列。"""
import hashlib
import secrets
import uuid

import bcrypt

from app.core.config import get_settings


def hash_password(password: str) -> str:
    settings = get_settings()
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_id() -> str:
    return uuid.uuid4().hex


def new_receipt_id() -> str:
    return "rcpt_" + secrets.token_hex(8)


def new_transfer_code() -> str:
    return secrets.token_hex(4).upper()


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
