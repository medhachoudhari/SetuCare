"""Audit logging. Snapshots are sanitised so patient names/secrets never reach the log."""
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog

_REDACT_KEYS = {"patient_name", "password", "hashed_password", "access_token", "referral_note",
                "conversation_summary"}


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k in _REDACT_KEYS else sanitize(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def log_action(db: Session, *, action: str, user_id: int | None = None, referral_id: int | None = None,
               input_snapshot: dict | None = None, output_snapshot: dict | None = None) -> AuditLog:
    """Adds an audit row to the caller's transaction (committed together with the change)."""
    entry = AuditLog(user_id=user_id, referral_id=referral_id, action=action,
                     input_snapshot=sanitize(input_snapshot) if input_snapshot is not None else None,
                     output_snapshot=sanitize(output_snapshot) if output_snapshot is not None else None)
    db.add(entry)
    return entry
