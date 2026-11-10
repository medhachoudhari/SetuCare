"""Dashboard aggregations: real SQL GROUP BY / COUNT queries, scoped to the caller's access."""
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.database import utcnow
from app.models.enums import (
    OPEN_STATUSES,
    SUCCESS_STATUSES,
    ContactOutcome,
    EscalationStatus,
    ReferralStatus,
    UrgencyLevel,
)
from app.models.escalation import Escalation
from app.models.followup import FollowUp
from app.models.referral import Referral
from app.models.user import User
from app.security.permissions import scope_referrals

_SUCCESS_CONTACT = (ContactOutcome.REACHED_WILL_ATTEND, ContactOutcome.REACHED_NEEDS_SUPPORT,
                    ContactOutcome.MESSAGE_DELIVERED)


def _ids(user: User):
    return scope_referrals(select(Referral.id), user).subquery()


def followup_stats(db: Session, user: User) -> dict:
    now = utcnow()
    ids = _ids(user)
    row = db.execute(
        select(
            func.count(FollowUp.id),
            func.count(FollowUp.attempted_at),
            func.coalesce(func.sum(case((FollowUp.attempted_at.is_(None) & (FollowUp.scheduled_at <= now), 1), else_=0)), 0),
            func.coalesce(func.sum(case((FollowUp.attempted_at.is_(None) & (FollowUp.scheduled_at > now), 1), else_=0)), 0),
            func.coalesce(func.sum(case((FollowUp.contact_outcome.in_(_SUCCESS_CONTACT), 1), else_=0)), 0),
        ).where(FollowUp.referral_id.in_(select(ids.c.id)))).one()
    total, attempted, due, upcoming, success = (int(x or 0) for x in row)
    return {"total": total, "attempted": attempted, "pending_due": due, "pending_upcoming": upcoming,
            "successful_contacts": success,
            "contact_success_rate": round(success / attempted, 4) if attempted else 0.0}


def summary(db: Session, user: User) -> dict:
    now = utcnow()
    base = scope_referrals(select(Referral.id), user).subquery()
    row = db.execute(select(
        func.count(Referral.id),
        func.coalesce(func.sum(case((Referral.status.in_(OPEN_STATUSES), 1), else_=0)), 0),
        func.coalesce(func.sum(case((Referral.status.in_(SUCCESS_STATUSES), 1), else_=0)), 0),
        func.coalesce(func.sum(case((Referral.status.in_(OPEN_STATUSES) & (Referral.deadline < now), 1), else_=0)), 0),
        func.coalesce(func.sum(case((Referral.status == ReferralStatus.CANCELLED, 1), else_=0)), 0),
    ).where(Referral.id.in_(select(base.c.id)))).one()
    total, pending, confirmed, overdue, cancelled = (int(x or 0) for x in row)
    pending_esc = db.scalar(select(func.count(Escalation.id)).where(
        Escalation.status == EscalationStatus.PENDING_REVIEW,
        Escalation.referral_id.in_(select(base.c.id)))) if user.role.value != "facility_staff" else 0
    denom = total - cancelled
    return {"total_referrals": total, "pending_referrals": pending, "confirmed_referrals": confirmed,
            "overdue_referrals": overdue, "pending_escalations": int(pending_esc or 0),
            "referral_confirmation_rate": round(confirmed / denom, 4) if denom > 0 else 0.0,
            "followups": followup_stats(db, user)}


def _group(db: Session, user: User, column) -> dict[str, int]:
    base = scope_referrals(select(Referral.id), user).subquery()
    rows = db.execute(select(column, func.count(Referral.id)).where(Referral.id.in_(select(base.c.id)))
                      .group_by(column)).all()
    return {(k.value if hasattr(k, "value") else str(k)): n for k, n in rows if k is not None}


def statistics(db: Session, user: User) -> dict:
    base = scope_referrals(select(Referral.id), user).subquery()
    by_status = {s.value: 0 for s in ReferralStatus} | _group(db, user, Referral.status)
    by_urgency = {u.value: 0 for u in UrgencyLevel} | _group(db, user, Referral.human_assigned_urgency)
    confirmed = db.execute(select(Referral.created_at, Referral.confirmed_at).where(
        Referral.id.in_(select(base.c.id)), Referral.confirmed_at.is_not(None))).all()
    hours = [(c - s).total_seconds() / 3600 for s, c in confirmed]
    return {"by_status": by_status, "by_urgency": by_urgency,
            "by_barrier_type": _group(db, user, Referral.barrier_type),
            "followups": followup_stats(db, user),
            "average_hours_to_confirmation": round(sum(hours) / len(hours), 2) if hours else None}


def _items(db: Session, user: User, stmt, limit: int) -> list[dict]:
    now = utcnow()
    out = []
    for r in db.scalars(scope_referrals(stmt, user).limit(limit)):
        out.append({"id": r.id, "referral_reference": r.referral_reference,
                    "patient_identifier": r.patient_identifier, "status": r.status,
                    "human_assigned_urgency": r.human_assigned_urgency, "deadline": r.deadline.isoformat(),
                    "is_overdue": r.deadline < now,
                    "hours_remaining": round((r.deadline - now).total_seconds() / 3600, 2),
                    "receiving_facility_id": r.receiving_facility_id,
                    "receiving_facility_name": r.receiving_facility.facility_name})
    return out


def urgent_referrals(db: Session, user: User, limit: int) -> list[dict]:
    stmt = (select(Referral).where(Referral.status.in_(OPEN_STATUSES),
                                   Referral.human_assigned_urgency.in_((UrgencyLevel.URGENT, UrgencyLevel.EMERGENCY)))
            .order_by(Referral.deadline))
    return _items(db, user, stmt, limit)


def pending_confirmations(db: Session, user: User, limit: int) -> list[dict]:
    stmt = select(Referral).where(Referral.status.in_(OPEN_STATUSES)).order_by(Referral.deadline)
    return _items(db, user, stmt, limit)
