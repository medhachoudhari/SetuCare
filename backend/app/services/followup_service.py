from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import utcnow
from app.integrations.ai_agents import (
    APPROVED_TEMPLATES,
    AIAgentError,
    AIAgentService,
    CommunicationPreferences,
    ContactStrategyRequest,
    ContactStrategyResponse,
    FollowUpAttemptSummary,
)
from app.models.enums import (
    OPEN_STATUSES,
    ContactChannel,
    ContactOutcome,
    NextAction,
    ReferralStatus,
    TimeOfDay,
    UserRole,
)
from app.models.followup import FollowUp
from app.models.referral import Referral
from app.models.user import User
from app.schemas.followup import FollowUpCreate, FollowUpUpdate
from app.services import audit_service, referral_service, scheduling_service
from app.services.errors import bad_request, conflict, forbidden, not_found

_FAILED = {ContactOutcome.NO_ANSWER, ContactOutcome.NUMBER_UNREACHABLE, ContactOutcome.MESSAGE_FAILED}


def require_followup_role(user: User) -> None:
    if user.role not in (UserRole.HEALTH_WORKER, UserRole.ADMINISTRATOR):
        raise forbidden("Only health workers and administrators can manage follow-ups")


def get_followup_for_user(db: Session, user: User, followup_id: int) -> FollowUp:
    require_followup_role(user)
    fu = db.get(FollowUp, followup_id)
    if fu is None:
        raise not_found("Follow-up")
    referral_service.get_for_user(db, user, fu.referral_id)  # 404 if out of scope
    return fu


def _attempt_summaries(referral: Referral) -> list[FollowUpAttemptSummary]:
    return [FollowUpAttemptSummary(attempt_number=f.attempt_number, channel=f.channel,
                                   contact_outcome=f.contact_outcome, attempted_at=f.attempted_at,
                                   conversation_summary=None)  # free text is never sent to the agent
            for f in referral.followups if f.attempted_at is not None]


def get_strategy(referral: Referral, ai: AIAgentService) -> tuple[ContactStrategyResponse | None, str | None]:
    """Agent 2. Returns (strategy, error). On failure the caller falls back to deterministic defaults."""
    prefs = CommunicationPreferences.model_validate(referral.communication_preferences or {})
    request = ContactStrategyRequest(referral_context=referral_service.build_context(referral),
                                     barrier_type=referral.barrier_type, previous_attempts=_attempt_summaries(referral),
                                     family_communication_preferences=prefs)
    try:
        return ai.recommend_contact_strategy(request), None
    except AIAgentError as exc:
        return None, str(exc)[:300]


def _build_followup(db: Session, user: User | None, referral: Referral, ai: AIAgentService,
                    channel: ContactChannel | None, scheduled_at: datetime | None) -> FollowUp:
    strategy, error = get_strategy(referral, ai)
    prefs = CommunicationPreferences.model_validate(referral.communication_preferences or {})
    if strategy:
        chosen = channel or strategy.recommended_channel
        template, review = strategy.message_template_id, strategy.requires_human_review
        ai_delay = strategy.recommended_delay_hours
    else:  # deterministic safe fallback; flagged for human review
        chosen = channel or prefs.preferred_channel or ContactChannel.PHONE_CALL
        template, review, ai_delay = "TPL_REFERRAL_REMINDER", True, None
    if scheduled_at is None:
        scheduled_at = scheduling_service.next_attempt_time(referral.human_assigned_urgency, utcnow(), ai_delay,
                                                            referral.deadline)
    number = (db.scalar(select(func.max(FollowUp.attempt_number)).where(FollowUp.referral_id == referral.id)) or 0) + 1
    fu = FollowUp(referral_id=referral.id, attempt_number=number, channel=chosen, scheduled_at=scheduled_at,
                  message_template_id=template, requires_human_review=review,
                  ai_recommendation=strategy.model_dump(mode="json") if strategy else {"error": error},
                  created_by=user.id if user else None)
    db.add(fu)
    db.flush()
    audit_service.log_action(db, action="ai.contact_strategy" if strategy else "ai.contact_strategy.failed",
                             user_id=user.id if user else None, referral_id=referral.id,
                             input_snapshot={"attempt_number": number},
                             output_snapshot=strategy.model_dump(mode="json") if strategy else {"error": error})
    return fu


def create_followup(db: Session, user: User, data: FollowUpCreate, ai: AIAgentService) -> FollowUp:
    require_followup_role(user)
    referral = referral_service.get_for_user(db, user, data.referral_id)
    if referral.status not in OPEN_STATUSES:
        raise conflict("Cannot schedule follow-ups for a closed referral", "referral_closed")
    if any(f.attempted_at is None for f in referral.followups):
        raise conflict("A follow-up is already pending for this referral", "followup_already_pending")
    if data.scheduled_at is not None and data.scheduled_at.tzinfo is None:
        raise bad_request("scheduled_at must include a timezone", "validation_error")
    fu = _build_followup(db, user, referral, ai, data.channel, data.scheduled_at)
    if referral.status == ReferralStatus.REFERRED:
        referral_service.transition(db, referral, ReferralStatus.FOLLOW_UP_IN_PROGRESS, actor=user,
                                    reason="First follow-up scheduled")
    audit_service.log_action(db, action="followup.create", user_id=user.id, referral_id=referral.id,
                             output_snapshot={"followup_id": fu.id, "attempt": fu.attempt_number,
                                              "channel": fu.channel, "scheduled_at": fu.scheduled_at})
    db.commit()
    return fu


def _decide_next(db: Session, referral: Referral, fu: FollowUp) -> NextAction:
    outcome = fu.contact_outcome
    attempted = sum(1 for f in referral.followups if f.attempted_at is not None)
    if outcome == ContactOutcome.REACHED_WILL_ATTEND:
        return NextAction.AWAIT_FACILITY_CONFIRMATION
    if outcome == ContactOutcome.REACHED_DECLINED:
        return NextAction.RECOMMEND_ESCALATION
    if attempted >= get_settings().max_followup_attempts:
        return NextAction.RECOMMEND_ESCALATION
    if outcome == ContactOutcome.NUMBER_UNREACHABLE:
        return NextAction.VERIFY_CONTACT_DETAILS
    if outcome == ContactOutcome.REACHED_NEEDS_SUPPORT:
        return NextAction.ARRANGE_SUPPORT_REVIEW
    return NextAction.SCHEDULE_NEXT_ATTEMPT


def update_followup(db: Session, user: User, fu: FollowUp, data: FollowUpUpdate,
                    ai: AIAgentService) -> tuple[FollowUp, FollowUp | None]:
    referral = referral_service.get_for_user(db, user, fu.referral_id)
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise bad_request("No fields to update", "validation_error")
    for key in ("attempted_at", "scheduled_at"):
        if changes.get(key) is not None and changes[key].tzinfo is None:
            raise bad_request(f"{key} must include a timezone", "validation_error")
    was_attempted = fu.attempted_at is not None
    if was_attempted and ("scheduled_at" in changes or "channel" in changes):
        raise conflict("A completed attempt cannot be rescheduled", "followup_already_attempted")
    if "scheduled_at" in changes and changes["scheduled_at"] is None:
        raise bad_request("scheduled_at cannot be null", "validation_error")
    now = utcnow()
    if changes.get("attempted_at") and changes["attempted_at"] > now:
        raise bad_request("attempted_at cannot be in the future", "validation_error")
    outcome = changes.get("contact_outcome")
    if outcome == ContactOutcome.PENDING:
        raise bad_request("Use a concrete contact_outcome when recording an attempt", "validation_error")
    before = {k: getattr(fu, k) for k in changes}
    for key, value in changes.items():
        setattr(fu, key, value)
    next_fu = None
    recording_now = (not was_attempted) and (outcome is not None or "attempted_at" in changes)
    if recording_now:
        if outcome is None:
            raise bad_request("contact_outcome is required when recording an attempt", "validation_error")
        if fu.attempted_at is None:
            fu.attempted_at = now
        if referral.status not in OPEN_STATUSES:
            raise conflict("Referral is closed", "referral_closed")
        db.flush()
        db.refresh(referral)
        action = _decide_next(db, referral, fu)
        fu.next_recommended_action = action
        if action in (NextAction.SCHEDULE_NEXT_ATTEMPT, NextAction.ARRANGE_SUPPORT_REVIEW,
                      NextAction.VERIFY_CONTACT_DETAILS) and not any(f.attempted_at is None for f in referral.followups):
            next_fu = _build_followup(db, user, referral, ai, None, None)
        if referral.status == ReferralStatus.REFERRED:
            referral_service.transition(db, referral, ReferralStatus.FOLLOW_UP_IN_PROGRESS, actor=user,
                                        reason="Follow-up attempt recorded")
    audit_service.log_action(db, action="followup.update", user_id=user.id, referral_id=referral.id,
                             input_snapshot=before,
                             output_snapshot={"followup_id": fu.id, "outcome": fu.contact_outcome,
                                              "next_action": fu.next_recommended_action,
                                              "next_followup_id": next_fu.id if next_fu else None})
    db.commit()
    return fu, next_fu


def serialize(fu: FollowUp) -> dict:
    from app.schemas.followup import FollowUpOut

    out = FollowUpOut.model_validate(fu).model_dump()
    out["message_preview"] = APPROVED_TEMPLATES.get(fu.message_template_id or "")
    return out
