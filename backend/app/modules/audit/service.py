"""审计日志：关键操作与变更理由（§12.2）。不记录密码或完整会话令牌。"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


async def audit(db: AsyncSession, actor_user_id: str | None, action: str,
                entity_type: str, entity_id: str | None, detail: dict | None = None) -> None:
    db.add(AuditLog(actor_user_id=actor_user_id, action=action, entity_type=entity_type,
                    entity_id=entity_id, detail=detail or {}))


async def list_logs(db: AsyncSession, action_prefix: str | None, limit: int, page: int) -> list[AuditLog]:
    from sqlalchemy import select
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if action_prefix:
        stmt = stmt.where(AuditLog.action.contains(action_prefix))
    stmt = stmt.limit(limit).offset((page - 1) * limit)
    return list((await db.execute(stmt)).scalars())
