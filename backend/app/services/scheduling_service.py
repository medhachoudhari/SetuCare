"""
Deterministic deadline / follow-up scheduling policy. NO AI calls happen here.

Safety rules encoded here:
  * The deadline comes only from the human-assigned urgency + configured policy.
  * AI-suggested delays can only make a follow-up EARLIER than the policy interval.
  * Escalation becomes mandatory once the deadline passes, regardless of any AI output.
"""
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import utcnow
from app.models.enums import OPEN_STATUSES, ContactOutcome, EscalationStatus, UrgencyLevel
from app.models.escalation import Escalation
from app.models.followup import FollowUp
from app.models.referral import Referral
from app.models.user import User
from app.security.permissions import scope_referrals


def calculate_deadline(urgency: UrgencyLevel, start: datetime | None = None) -> datetime:
    start = start or utcnow()
    return start + timedelta(hours=get_settings().deadline_hours[urgency.value])


def max_followup_interval_hours(urgency: UrgencyLevel) -> float:
    return get_settings().followup_interval_hours[urgency.value]


def next_attempt_time(urgency: UrgencyLevel, now: datetime | None = None, ai_delay_hours: float | None = None,
                      deadline: datetime | None = None) -> datetime:
    """Policy interval is a ceiling; the AI may only shorten it. Never scheduled past the deadline."""
    now = now or utcnow()
    delay = max_followup_interval_hours(urgency)
    if ai_delay_hours is not None:
        delay = min(delay, max(0.0, ai_delay_hours))
    at = now + timedelta(hours=delay)
    if deadline is not None and deadline > now:
        at = min(at, deadline)
    return at


def is_overdue(referral: Referral, now: datetime | None = None) -> bool:
    return referral.status in OPEN_STATUSES and referral.deadline < (now or utcnow())


def is_escalation_mandatory(referral: Referral, now: datetime | None = None) -> bool:
    return is_overdue(referral, now)


def find_overdue_referrals(db: Session, user: User | None = None, now: datetime | None = None) -> list[Referral]:
    stmt = select(Referral).where(Referral.status.in_(OPEN_STATUSES), Referral.deadline < (now or utcnow()))
    if user is not None:
        stmt = scope_referrals(stmt, user)
    return list(db.scalars(stmt.order_by(Referral.deadline)))


def find_pending_followups(db: Session, user: User | None = None, now: datetime | None = None,
                           include_upcoming: bool = False) -> list[FollowUp]:
    """Un-attempted follow-ups that are due now (or all un-attempted when include_upcoming)."""
    now = now or utcnow()
    stmt = (select(FollowUp).join(Referral, FollowUp.referral_id == Referral.id)
            .where(FollowUp.attempted_at.is_(None), Referral.status.in_(OPEN_STATUSES)))
    if not include_upcoming:
        stmt = stmt.where(FollowUp.scheduled_at <= now)
    if user is not None:
        stmt = scope_referrals(stmt, user)
    return list(db.scalars(stmt.order_by(FollowUp.scheduled_at)))


def exhausted_attempts_referral_ids(db: Session) -> set[int]:
    """Open referrals where max attempts were made without anyone confirming they will attend."""
    s = get_settings()
    attempted = (select(FollowUp.referral_id, func.count(FollowUp.id).label("n"))
                 .where(FollowUp.attempted_at.is_not(None)).group_by(FollowUp.referral_id).subquery())
    reached = exists().where(and_(FollowUp.referral_id == Referral.id,
                                  FollowUp.contact_outcome == ContactOutcome.REACHED_WILL_ATTEND))
    stmt = (select(Referral.id).join(attempted, attempted.c.referral_id == Referral.id)
            .where(Referral.status.in_(OPEN_STATUSES), attempted.c.n >= s.max_followup_attempts, ~reached))
    return set(db.scalars(stmt))


def escalation_candidates(db: Session, now: datetime | None = None) -> list[Referral]:
    """Open referrals needing an escalation recommendation (overdue or attempts exhausted),
    which do not already have a pending escalation."""
    now = now or utcnow()
    ids = {r.id for r in find_overdue_referrals(db, now=now)} | exhausted_attempts_referral_ids(db)
    if not ids:
        return []
    has_pending = exists().where(and_(Escalation.referral_id == Referral.id,
                                      Escalation.status == EscalationStatus.PENDING_REVIEW))
    stmt = select(Referral).where(Referral.id.in_(ids), Referral.status.in_(OPEN_STATUSES), ~has_pending)
    return list(db.scalars(stmt.order_by(Referral.deadline)))


def run_escalation_sweep(db: Session) -> int:
    """Scheduler job: create PENDING escalation recommendations. Never approves anything."""
    from app.integrations.ai_agents import get_ai_service
    from app.services import escalation_service

    created = 0
    for referral in escalation_candidates(db):
        try:
            escalation_service.create_escalation(db, referral, user=None, ai=get_ai_service())
            db.commit()
            created += 1
        except Exception:  # one bad referral must not stop the sweep
            db.rollback()
    return created
