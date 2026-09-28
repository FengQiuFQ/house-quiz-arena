"""审计查询（管理端）。"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_admin
from app.db.models import User
from app.modules.audit.service import list_logs

router = APIRouter(prefix="/api/v1", tags=["audit"])


@router.get("/admin/audit")
async def audit_list(prefix: str | None = None, page: int = 1, limit: int = 50,
                     _admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_db)) -> dict:
    rows = await list_logs(db, prefix, limit, page)
    return {"items": [{"id": a.id, "actor": a.actor_user_id, "action": a.action,
                       "entity_type": a.entity_type, "entity_id": a.entity_id,
                       "detail": a.detail, "created_at": a.created_at} for a in rows],
            "page": page, "limit": limit}
