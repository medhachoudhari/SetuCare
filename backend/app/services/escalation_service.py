from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import utcnow
from app.integrations.ai_agents import (
    AIAgentError,
    AIAgentService,
    BarrierSummary,
    CaseEvent,
    EscalationSynthesisRequest,
    FollowUpAttemptSummary,
)
from app.models.enums import (
    OPEN_STATUSES,
    URGENCY_RANK,
    EscalationStatus,
    ReferralStatus,
    UrgencyLevel,
    max_urgency,
)
from app.models.escalation import Escalation
from app.models.referral import Referral
from app.models.user import User
from app.security.permissions import can_review_escalation, scope_referrals
from app.services import audit_service, referral_service
from app.services.errors import conflict, forbidden, not_found


def get_for_user(db: Session, user: User, escalation_id: int) -> Escalation:
    esc = db.get(Escalation, escalation_id)
    if esc is None:
        raise not_found("Escalation")
    referral_service.get_for_user(db, user, esc.referral_id)  # scope check (404 if not allowed)
    if user.role.value == "facility_staff":
        raise forbidden("Facility staff cannot view escalations")
    return esc


def list_pending(db: Session, user: User) -> list[Escalation]:
    if user.role.value == "facility_staff":
        raise forbidden("Facility staff cannot view escalations")
    stmt = (scope_referrals(select(Escalation).join(Referral, Escalation.referral_id == Referral.id), user)
            .where(Escalation.status == EscalationStatus.PENDING_REVIEW).order_by(Escalation.created_at))
    return list(db.scalars(stmt))


def create_escalation(db: Session, referral: Referral, user: User | None, ai: AIAgentService) -> Escalation:
    """Agent 3 produces a RECOMMENDATION that always starts as pending_review.
    user=None means the scheduler created it; nothing is ever auto-approved."""
    if referral.status not in OPEN_STATUSES:
        raise conflict("Cannot escalate a closed referral", "referral_closed")
    if referral.status == ReferralStatus.ESCALATION_PENDING or db.scalar(
            select(Escalation.id).where(Escalation.referral_id == referral.id,
                                        Escalation.status == EscalationStatus.PENDING_REVIEW)):
        raise conflict("An escalation is already pending review for this referral", "escalation_already_pending")

    request = EscalationSynthesisRequest(
        referral_context=referral_service.build_context(referral), referral_note=referral.referral_note,
        barrier_interpretation=BarrierSummary(
            barrier_type=referral.barrier_type, explanation=referral.barrier_reasoning,
            confidence=referral.barrier_confidence) if referral.barrier_type else None,
        followup_history=[FollowUpAttemptSummary(attempt_number=f.attempt_number, channel=f.channel,
                                                 contact_outcome=f.contact_outcome, attempted_at=f.attempted_at)
                          for f in referral.followups if f.attempted_at is not None],
        case_events=[CaseEvent(timestamp=h.changed_at, event_type=f"status:{h.to_status.value}",
                               description=h.reason or "") for h in referral.status_history])
    human = referral.human_assigned_urgency
    try:
        result = ai.synthesize_escalation(request)
        ai_record = result.model_dump(mode="json")
        brief, reason = result.case_summary, result.escalation_reason
        action, uncertain = result.recommended_action.value, result.uncertainty_flag
        ai_urgency = result.urgency_recommendation
    except AIAgentError as exc:  # deterministic fallback so a safety escalation is never lost
        ai_record = {"error": str(exc)[:300], "fallback": True}
        brief = (f"Automated synthesis unavailable. Referral {referral.referral_reference} requires manual "
                 f"review (status {referral.status.value}).")
        reason, action, uncertain, ai_urgency = "Escalation requested; AI synthesis unavailable", "supervisor_review", True, human

    # SAFETY: the AI may raise urgency but can never lower the human-assigned level.
    final_urgency = max_urgency(human, ai_urgency)
    downgrade_blocked = URGENCY_RANK[ai_urgency] < URGENCY_RANK[human]
    esc = Escalation(referral_id=referral.id, ai_generated_brief=brief, reason=reason, recommended_action=action,
                     urgency_level=final_urgency, status=EscalationStatus.PENDING_REVIEW,
                     ai_recommendation=ai_record, uncertainty_flag=uncertain,
                     created_by=user.id if user else None)
    db.add(esc)
    db.flush()
    referral_service.transition(db, referral, ReferralStatus.ESCALATION_PENDING, actor=user,
                                reason="Escalation recommended; awaiting human review")
    audit_service.log_action(db, action="ai.escalation_synthesis", user_id=user.id if user else None,
                             referral_id=referral.id, input_snapshot={"human_urgency": human},
                             output_snapshot=ai_record)
    audit_service.log_action(db, action="escalation.create", user_id=user.id if user else None,
                             referral_id=referral.id,
                             output_snapshot={"escalation_id": esc.id, "urgency": final_urgency,
                                              "ai_urgency_downgrade_blocked": downgrade_blocked})
    if downgrade_blocked:
        audit_service.log_action(db, action="safety.urgency_downgrade_blocked", user_id=None,
                                 referral_id=referral.id,
                                 input_snapshot={"human_urgency": human, "ai_urgency": ai_urgency},
                                 output_snapshot={"applied_urgency": final_urgency})
    db.flush()
    return esc


def _review(db: Session, user: User, esc: Escalation, approve: bool, notes: str | None) -> Escalation:
    referral = referral_service.get_for_user(db, user, esc.referral_id)
    if not can_review_escalation(user, referral):
        raise forbidden("You are not authorised to review this escalation")
    if esc.status != EscalationStatus.PENDING_REVIEW:
        raise conflict(f"Escalation already {esc.status.value}", "escalation_already_reviewed")
    esc.status = EscalationStatus.APPROVED if approve else EscalationStatus.REJECTED
    esc.reviewed_by, esc.reviewed_at, esc.reviewer_notes = user.id, utcnow(), notes
    if referral.status == ReferralStatus.ESCALATION_PENDING:
        if approve:
            referral_service.transition(db, referral, ReferralStatus.ESCALATED, actor=user,
                                        reason="Escalation approved by human reviewer")
        else:
            previous = next((h.from_status for h in reversed(referral.status_history)
                             if h.to_status == ReferralStatus.ESCALATION_PENDING), ReferralStatus.REFERRED)
            referral_service.transition(db, referral, previous or ReferralStatus.REFERRED, actor=user,
                                        reason="Escalation rejected by human reviewer")
    audit_service.log_action(db, action="escalation.approve" if approve else "escalation.reject",
                             user_id=user.id, referral_id=referral.id,
                             input_snapshot={"escalation_id": esc.id, "notes": notes},
                             output_snapshot={"status": esc.status, "reviewed_at": esc.reviewed_at,
                                              "referral_status": referral.status})
    db.commit()
    return esc


def approve(db: Session, user: User, esc: Escalation, notes: str | None = None) -> Escalation:
    return _review(db, user, esc, True, notes)


def reject(db: Session, user: User, esc: Escalation, notes: str | None) -> Escalation:
    return _review(db, user, esc, False, notes)
