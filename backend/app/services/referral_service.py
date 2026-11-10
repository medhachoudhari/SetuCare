import secrets
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import utcnow
from app.integrations.ai_agents import (
    AIAgentError,
    AIAgentService,
    BarrierInterpretationRequest,
    ReferralContext,
)
from app.models.enums import (
    OPEN_STATUSES,
    BarrierType,
    EscalationStatus,
    ReferralOutcome,
    ReferralStatus,
    UrgencyLevel,
    UserRole,
)
from app.models.escalation import Escalation
from app.models.facility import Facility
from app.models.referral import Referral, ReferralStatusHistory
from app.models.user import User
from app.schemas.referral import ArrivalConfirmation, ReferralCreate, ReferralUpdate, StatusChange
from app.security.permissions import can_access_referral, can_view_patient_name, scope_referrals
from app.services import audit_service, scheduling_service
from app.services.errors import bad_request, conflict, forbidden, not_found

_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters

S = ReferralStatus
# Allowed transitions (enforced for everyone).
ALLOWED_TRANSITIONS: dict[ReferralStatus, set[ReferralStatus]] = {
    S.REFERRED: {S.FOLLOW_UP_IN_PROGRESS, S.ESCALATION_PENDING, S.CONFIRMED, S.CANCELLED, S.LOST_TO_FOLLOW_UP},
    S.FOLLOW_UP_IN_PROGRESS: {S.ESCALATION_PENDING, S.CONFIRMED, S.CANCELLED, S.LOST_TO_FOLLOW_UP},
    S.ESCALATION_PENDING: {S.ESCALATED, S.REFERRED, S.FOLLOW_UP_IN_PROGRESS, S.CONFIRMED, S.CANCELLED,
                           S.LOST_TO_FOLLOW_UP},
    S.ESCALATED: {S.FOLLOW_UP_IN_PROGRESS, S.CONFIRMED, S.CANCELLED, S.LOST_TO_FOLLOW_UP},
    S.CONFIRMED: {S.COMPLETED},
    S.COMPLETED: set(), S.CANCELLED: set(), S.LOST_TO_FOLLOW_UP: set(),
}
# Which roles may request each target status through the API.
HUMAN_STATUS_ROLES: dict[ReferralStatus, set[UserRole]] = {
    S.FOLLOW_UP_IN_PROGRESS: {UserRole.HEALTH_WORKER, UserRole.ADMINISTRATOR},
    S.CANCELLED: {UserRole.HEALTH_WORKER, UserRole.ADMINISTRATOR},
    S.LOST_TO_FOLLOW_UP: {UserRole.HEALTH_WORKER, UserRole.ADMINISTRATOR},
    S.CONFIRMED: {UserRole.FACILITY_STAFF, UserRole.ADMINISTRATOR},
    S.COMPLETED: {UserRole.FACILITY_STAFF, UserRole.ADMINISTRATOR},
}
# Only a human facility user/admin can mark success; escalation states belong to the escalation workflow.
HUMAN_ONLY_TARGETS = {S.CONFIRMED, S.COMPLETED}
WORKFLOW_ONLY_TARGETS = {S.ESCALATION_PENDING, S.ESCALATED, S.REFERRED}
SUPERSEDING_TARGETS = {S.CONFIRMED, S.CANCELLED, S.LOST_TO_FOLLOW_UP, S.COMPLETED}


def generate_reference(db: Session) -> str:
    for _ in range(10):
        ref = f"SC-{utcnow():%Y%m%d}-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))
        if not db.scalar(select(Referral.id).where(Referral.referral_reference == ref)):
            return ref
    raise conflict("Could not generate a unique referral reference", "reference_generation_failed")


def build_context(referral: Referral, now: datetime | None = None) -> ReferralContext:
    now = now or utcnow()
    return ReferralContext(
        referral_reference=referral.referral_reference, status=referral.status,
        human_assigned_urgency=referral.human_assigned_urgency, age=referral.age, deadline=referral.deadline,
        hours_until_deadline=round((referral.deadline - now).total_seconds() / 3600, 2),
        referring_facility_type=referral.referring_facility.facility_type.value if referral.referring_facility else None,
        receiving_facility_type=referral.receiving_facility.facility_type.value if referral.receiving_facility else None)


def _add_history(db: Session, referral: Referral, from_status, to_status, actor: User | None, reason: str | None):
    db.add(ReferralStatusHistory(referral=referral, from_status=from_status, to_status=to_status,
                                 changed_by=actor.id if actor else None, reason=reason))


def get_for_user(db: Session, user: User, referral_id: int) -> Referral:
    """404 (not 403) for records outside the user's scope, so existence is not leaked."""
    referral = db.get(Referral, referral_id)
    if referral is None or not can_access_referral(user, referral):
        raise not_found("Referral")
    return referral


def run_barrier_analysis(db: Session, referral: Referral, ai: AIAgentService, user: User | None) -> None:
    """Agent 1. Updates ONLY barrier fields; urgency/status are never touched by AI output."""
    request = BarrierInterpretationRequest(
        referral_note=referral.referral_note, human_assigned_urgency=referral.human_assigned_urgency,
        referral_context=build_context(referral))
    try:
        result = ai.interpret_barrier(request)
    except AIAgentError as exc:
        referral.barrier_type = BarrierType.UNKNOWN
        referral.barrier_reasoning = "Automated barrier analysis unavailable; manual review required."
        referral.barrier_confidence = None
        referral.barrier_needs_review = True
        audit_service.log_action(db, action="ai.barrier_interpretation.failed", user_id=user.id if user else None,
                                 referral_id=referral.id, output_snapshot={"error": str(exc)[:300]})
        return
    referral.barrier_type = result.barrier_type
    referral.barrier_reasoning = result.explanation + f" Suggested action: {result.recommended_logistical_action}"
    referral.barrier_confidence = result.confidence
    referral.barrier_needs_review = result.uncertainty_flag
    audit_service.log_action(db, action="ai.barrier_interpretation", user_id=user.id if user else None,
                             referral_id=referral.id,
                             input_snapshot={"urgency": referral.human_assigned_urgency},
                             output_snapshot=result.model_dump(mode="json"))


def create_referral(db: Session, user: User, data: ReferralCreate, ai: AIAgentService) -> Referral:
    if user.role == UserRole.FACILITY_STAFF:
        raise forbidden("Facility staff cannot create referrals")
    if user.role == UserRole.HEALTH_WORKER:
        # Never trust client-supplied ownership: derive from the authenticated user.
        if data.referring_facility_id is not None or data.assigned_health_worker_id is not None:
            raise forbidden("Referring facility and assigned health worker are set by the server")
        if user.facility_id is None:
            raise bad_request("Your account is not assigned to a facility; contact an administrator",
                              "no_facility")
        referring_id, worker_id = user.facility_id, user.id
    else:
        if data.referring_facility_id is None:
            raise bad_request("referring_facility_id is required for administrators", "validation_error")
        referring_id, worker_id = data.referring_facility_id, data.assigned_health_worker_id
        if worker_id is not None:
            worker = db.get(User, worker_id)
            if worker is None or worker.role != UserRole.HEALTH_WORKER:
                raise bad_request("assigned_health_worker_id must be an existing health worker", "validation_error")
    if referring_id == data.receiving_facility_id:
        raise bad_request("Referring and receiving facilities must differ", "validation_error")
    for fid in (referring_id, data.receiving_facility_id):
        if db.get(Facility, fid) is None:
            raise bad_request(f"Facility {fid} does not exist", "validation_error")

    now = utcnow()
    prefs = data.communication_preferences.model_dump(mode="json") if data.communication_preferences else None
    for _ in range(3):
        referral = Referral(
            referral_reference=generate_reference(db), patient_identifier=data.patient_identifier,
            patient_name=data.patient_name, age=data.age, referring_facility_id=referring_id,
            receiving_facility_id=data.receiving_facility_id, assigned_health_worker_id=worker_id,
            human_assigned_urgency=data.human_assigned_urgency, referral_note=data.referral_note,
            communication_preferences=prefs, status=ReferralStatus.REFERRED,
            deadline=scheduling_service.calculate_deadline(data.human_assigned_urgency, now),
            created_at=now, updated_at=now)
        db.add(referral)
        try:
            db.flush()
            break
        except IntegrityError:  # reference collision under concurrency
            db.rollback()
            continue
    else:
        raise conflict("Could not create a unique referral reference", "reference_generation_failed")
    _add_history(db, referral, None, ReferralStatus.REFERRED, user, "Referral created")
    run_barrier_analysis(db, referral, ai, user)
    audit_service.log_action(db, action="referral.create", user_id=user.id, referral_id=referral.id,
                             input_snapshot=data.model_dump(mode="json", exclude={"referral_note", "patient_name"}),
                             output_snapshot={"reference": referral.referral_reference,
                                              "deadline": referral.deadline, "status": referral.status})
    db.commit()
    return referral


def list_referrals(db: Session, user: User, *, page: int, page_size: int, status: ReferralStatus | None = None,
                   urgency: UrgencyLevel | None = None, barrier_type: BarrierType | None = None,
                   overdue: bool | None = None, q: str | None = None, reference: str | None = None):
    stmt = scope_referrals(select(Referral), user)
    now = utcnow()
    if status:
        stmt = stmt.where(Referral.status == status)
    if urgency:
        stmt = stmt.where(Referral.human_assigned_urgency == urgency)
    if barrier_type:
        stmt = stmt.where(Referral.barrier_type == barrier_type)
    if reference:
        stmt = stmt.where(Referral.referral_reference == reference.strip().upper())
    if overdue is True:
        stmt = stmt.where(Referral.status.in_(OPEN_STATUSES), Referral.deadline < now)
    elif overdue is False:
        stmt = stmt.where(~(Referral.status.in_(OPEN_STATUSES) & (Referral.deadline < now)))
    if q:
        like = f"%{q.strip().replace('%', '').replace('_', '')}%"
        stmt = stmt.where(or_(Referral.referral_reference.ilike(like), Referral.patient_identifier.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Referral.created_at.desc(), Referral.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return list(rows), total


def update_referral(db: Session, user: User, referral: Referral, data: ReferralUpdate, ai: AIAgentService) -> Referral:
    if user.role == UserRole.FACILITY_STAFF:
        raise forbidden("Facility staff cannot edit referrals")
    if referral.status not in OPEN_STATUSES:
        raise conflict("Closed referrals cannot be edited", "referral_closed")
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise bad_request("No fields to update", "validation_error")
    before = {k: getattr(referral, k) for k in changes}
    if "receiving_facility_id" in changes:
        rid = changes["receiving_facility_id"]
        if rid == referral.referring_facility_id or db.get(Facility, rid) is None:
            raise bad_request("Invalid receiving facility", "validation_error")
    note_changed = "referral_note" in changes and changes["referral_note"] != referral.referral_note
    for key, value in changes.items():
        if key == "communication_preferences" and value is not None:
            value = data.communication_preferences.model_dump(mode="json")
        setattr(referral, key, value)
    if "human_assigned_urgency" in changes and changes["human_assigned_urgency"] != before["human_assigned_urgency"]:
        # Humans may re-grade urgency; the deadline is recomputed deterministically from creation time.
        referral.deadline = scheduling_service.calculate_deadline(referral.human_assigned_urgency, referral.created_at)
    if note_changed:
        run_barrier_analysis(db, referral, ai, user)
    audit_service.log_action(db, action="referral.update", user_id=user.id, referral_id=referral.id,
                             input_snapshot={k: v for k, v in before.items()},
                             output_snapshot={k: getattr(referral, k) for k in changes})
    db.commit()
    return referral


def transition(db: Session, referral: Referral, new_status: ReferralStatus, *, actor: User | None,
               reason: str | None = None) -> None:
    """Low-level, validated transition used by every workflow. `actor=None` means system/AI-driven,
    which can NEVER confirm or complete a referral."""
    if new_status in HUMAN_ONLY_TARGETS and actor is None:
        raise forbidden("Only a human user may confirm or close a referral")
    if new_status not in ALLOWED_TRANSITIONS[referral.status]:
        raise conflict(f"Cannot move referral from '{referral.status.value}' to '{new_status.value}'",
                       "invalid_status_transition")
    old = referral.status
    referral.status = new_status
    referral.updated_at = utcnow()
    _add_history(db, referral, old, new_status, actor, reason)
    if new_status in SUPERSEDING_TARGETS:
        for esc in db.scalars(select(Escalation).where(Escalation.referral_id == referral.id,
                                                       Escalation.status == EscalationStatus.PENDING_REVIEW)):
            esc.status = EscalationStatus.SUPERSEDED
            audit_service.log_action(db, action="escalation.superseded", user_id=actor.id if actor else None,
                                     referral_id=referral.id, output_snapshot={"escalation_id": esc.id,
                                                                                "closing_status": new_status})


def change_status(db: Session, user: User, referral: Referral, data: StatusChange) -> Referral:
    target = data.status
    if target in WORKFLOW_ONLY_TARGETS:
        raise forbidden(f"Status '{target.value}' is managed by the escalation workflow")
    if user.role not in HUMAN_STATUS_ROLES.get(target, set()):
        raise forbidden(f"Your role cannot set status '{target.value}'")
    if (referral.status == ReferralStatus.ESCALATION_PENDING and target == ReferralStatus.FOLLOW_UP_IN_PROGRESS):
        raise conflict("Review the pending escalation first", "escalation_pending")
    if target == ReferralStatus.COMPLETED and data.outcome is None:
        raise bad_request("outcome is required to complete a referral", "validation_error")
    if target != ReferralStatus.COMPLETED and data.outcome is not None:
        raise bad_request("outcome may only be set when completing a referral", "validation_error")
    old = referral.status
    transition(db, referral, target, actor=user, reason=data.reason)
    if target == ReferralStatus.CONFIRMED:
        referral.confirmed_at = utcnow()
        referral.confirmed_by = user.id
    if target == ReferralStatus.COMPLETED:
        referral.outcome, referral.outcome_notes = data.outcome, data.outcome_notes
    audit_service.log_action(db, action="referral.status_change", user_id=user.id, referral_id=referral.id,
                             input_snapshot={"from": old, "to": target, "reason": data.reason,
                                             "outcome": data.outcome},
                             output_snapshot={"status": referral.status})
    db.commit()
    return referral


def confirm_arrival(db: Session, user: User, referral: Referral, data: ArrivalConfirmation) -> Referral:
    if user.role not in HUMAN_STATUS_ROLES[ReferralStatus.CONFIRMED]:
        raise forbidden("Only facility staff or administrators can confirm patient arrival")
    now = utcnow()
    arrival = data.arrival_time or now
    if arrival.tzinfo is None:
        raise bad_request("arrival_time must include a timezone", "validation_error")
    if arrival > now:
        raise bad_request("arrival_time cannot be in the future", "validation_error")
    old = referral.status
    transition(db, referral, ReferralStatus.CONFIRMED, actor=user, reason=data.notes or "Arrival confirmed")
    referral.confirmed_at, referral.confirmed_by = arrival, user.id
    audit_service.log_action(db, action="referral.arrival_confirmed", user_id=user.id, referral_id=referral.id,
                             input_snapshot={"from": old, "arrival_time": arrival},
                             output_snapshot={"status": referral.status})
    db.commit()
    return referral


def serialize(referral: Referral, user: User) -> dict:
    """Role-aware projection: strips patient name and internal AI fields for non-authorised roles."""
    from app.schemas.referral import ReferralOut

    out = ReferralOut.model_validate(referral).model_dump()
    out["referring_facility_name"] = referral.referring_facility.facility_name if referral.referring_facility else None
    out["receiving_facility_name"] = referral.receiving_facility.facility_name if referral.receiving_facility else None
    out["is_overdue"] = scheduling_service.is_overdue(referral)
    if not can_view_patient_name(user, referral):
        out["patient_name"] = None
    if user.role == UserRole.FACILITY_STAFF:  # minimum necessary: no internal barrier analysis or family preferences
        for key in ("barrier_type", "barrier_reasoning", "barrier_confidence", "communication_preferences"):
            out[key] = None
        out["barrier_needs_review"] = False
    return out
