from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.audit import AuditLogOut
from app.schemas.common import Page, make_page
from app.security.permissions import require_roles

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


@router.get("/", response_model=Page[AuditLogOut], summary="Audit trail (administrator only)")
def list_audit_logs(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                    referral_id: int | None = None, action: str | None = None, user_id: int | None = None,
                    db: Session = Depends(get_db), _: User = Depends(require_roles(UserRole.ADMINISTRATOR))):
    stmt = select(AuditLog)
    if referral_id is not None:
        stmt = stmt.where(AuditLog.referral_id == referral_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page(rows, total, page, page_size)
