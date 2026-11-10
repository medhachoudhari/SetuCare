"""
AI agent integration layer.

Member 2 implements the real reasoning agents by subclassing ``AIAgentProvider`` and pointing
``AI_AGENT_PROVIDER=package.module:ClassName`` at it. The backend only ever talks to the
``AIAgentService`` gateway, which

  * validates every request and response with the Pydantic models below,
  * rejects malformed / unsafe output (``InvalidAIResponseError``),
  * forces human review on low-confidence output,
  * never lets AI output change urgency, close referrals or send free-text patient messages.

Patient-facing messages are limited to the approved logistical templates in ``APPROVED_TEMPLATES``.
No patient name or direct identifier is ever passed to an agent (only the pseudonymous reference).
"""
from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import get_settings
from app.models.enums import (
    BarrierType,
    ContactChannel,
    ContactOutcome,
    EscalationAction,
    ReferralStatus,
    TimeOfDay,
    UrgencyLevel,
)

logger = logging.getLogger("setucare.ai")

# --------------------------------------------------------------------------------------
# Approved patient-facing templates (logistical only; no medical advice).
# --------------------------------------------------------------------------------------
APPROVED_TEMPLATES: dict[str, str] = {
    "TPL_REFERRAL_REMINDER": "Reminder: a visit to the referred facility has been arranged for you. "
                             "Please reply or call if you need help reaching it.",
    "TPL_TRANSPORT_INFO": "We can help you with information about transport to the referred facility. "
                          "Please call the health worker to discuss options.",
    "TPL_FINANCIAL_SUPPORT_INFO": "Information about available support schemes for referral visits is "
                                  "available. Please contact the health worker to learn more.",
    "TPL_REASSURANCE_SUPPORT": "Your health worker is available to answer questions about the visit "
                               "and help with arrangements. Please get in touch.",
    "TPL_CLARIFY_INSTRUCTIONS": "Please contact the health worker to confirm the facility name, "
                                "address, and the documents to bring for your visit.",
    "TPL_REQUEST_CALLBACK": "We tried to reach you about your referral. Please call back at your "
                            "convenience.",
}


class AIAgentError(Exception):
    """Provider failed / unavailable."""


class InvalidAIResponseError(AIAgentError):
    """Provider output failed schema or safety validation."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------------------
# Shared context models (no direct patient identifiers)
# --------------------------------------------------------------------------------------
class ReferralContext(_Strict):
    referral_reference: str
    status: ReferralStatus
    human_assigned_urgency: UrgencyLevel
    age: int | None = None
    deadline: datetime
    hours_until_deadline: float | None = None
    referring_facility_type: str | None = None
    receiving_facility_type: str | None = None


class FollowUpAttemptSummary(_Strict):
    attempt_number: int
    channel: ContactChannel
    contact_outcome: ContactOutcome
    attempted_at: datetime | None = None
    conversation_summary: str | None = None


class CommunicationPreferences(_Strict):
    preferred_channel: ContactChannel | None = None
    preferred_time_of_day: TimeOfDay | None = None
    language: str | None = None
    do_not_contact_channels: list[ContactChannel] = Field(default_factory=list)


class CaseEvent(_Strict):
    timestamp: datetime
    event_type: str
    description: str


# --------------------------------------------------------------------------------------
# AGENT 1: Barrier interpretation
# --------------------------------------------------------------------------------------
class BarrierInterpretationRequest(_Strict):
    referral_note: str = Field(min_length=1, max_length=5000)
    human_assigned_urgency: UrgencyLevel
    referral_context: ReferralContext


class BarrierInterpretationResponse(_Strict):
    barrier_type: BarrierType
    extracted_evidence: list[str] = Field(default_factory=list, max_length=20)
    explanation: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    uncertainty_flag: bool
    recommended_logistical_action: str = Field(min_length=1, max_length=500)


# --------------------------------------------------------------------------------------
# AGENT 2: Adaptive contact strategy
# --------------------------------------------------------------------------------------
class ContactStrategyRequest(_Strict):
    referral_context: ReferralContext
    barrier_type: BarrierType | None = None
    previous_attempts: list[FollowUpAttemptSummary] = Field(default_factory=list)
    family_communication_preferences: CommunicationPreferences = Field(default_factory=CommunicationPreferences)


class ContactStrategyResponse(_Strict):
    recommended_channel: ContactChannel
    recommended_delay_hours: float = Field(ge=0, le=168, description="Hours from now; backend caps it by policy")
    preferred_time_of_day: TimeOfDay = TimeOfDay.ANY
    message_template_id: str
    reasoning: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_review: bool

    @field_validator("message_template_id")
    @classmethod
    def _approved_template(cls, v: str) -> str:
        if v not in APPROVED_TEMPLATES:
            raise ValueError(f"message_template_id '{v}' is not an approved template")
        return v


# --------------------------------------------------------------------------------------
# AGENT 3: Escalation synthesis
# --------------------------------------------------------------------------------------
class BarrierSummary(_Strict):
    barrier_type: BarrierType | None = None
    explanation: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class EscalationSynthesisRequest(_Strict):
    referral_context: ReferralContext
    referral_note: str = Field(max_length=5000)
    barrier_interpretation: BarrierSummary | None = None
    followup_history: list[FollowUpAttemptSummary] = Field(default_factory=list)
    case_events: list[CaseEvent] = Field(default_factory=list)


class EscalationSynthesisResponse(_Strict):
    case_summary: str = Field(min_length=1, max_length=4000)
    escalation_reason: str = Field(min_length=1, max_length=2000)
    recommended_action: EscalationAction
    urgency_recommendation: UrgencyLevel
    supporting_evidence: list[str] = Field(default_factory=list, max_length=30)
    uncertainty_flag: bool


# --------------------------------------------------------------------------------------
# Provider interface (Member 2 implements this)
# --------------------------------------------------------------------------------------
class AIAgentProvider(ABC):
    """Each method may return the response model or an equivalent dict; the gateway validates."""

    @abstractmethod
    def interpret_barrier(self, request: BarrierInterpretationRequest) -> BarrierInterpretationResponse | dict: ...

    @abstractmethod
    def recommend_contact_strategy(self, request: ContactStrategyRequest) -> ContactStrategyResponse | dict: ...

    @abstractmethod
    def synthesize_escalation(self, request: EscalationSynthesisRequest) -> EscalationSynthesisResponse | dict: ...


# --------------------------------------------------------------------------------------
# Mock provider (deterministic, keyword based; no external calls)
# --------------------------------------------------------------------------------------
_KEYWORDS: dict[BarrierType, tuple[str, ...]] = {
    BarrierType.FINANCIAL: ("money", "afford", "cost", "fee", "expens", "cannot pay", "can't pay", "wage", "loan", "poor"),
    BarrierType.TRANSPORTATION: ("transport", "bus", "vehicle", "ambulance", "road", "far from", "no ride", "travel"),
    BarrierType.FEAR: ("afraid", "fear", "scared", "anxious", "worried", "nervous", "frighten"),
    BarrierType.CONFUSION: ("confus", "don't understand", "do not understand", "unclear", "did not know",
                            "unaware", "language", "misunderstood"),
    BarrierType.FAMILY_SOCIAL: ("family", "husband", "mother-in-law", "childcare", "no one to accompany",
                                "caregiver", "permission", "elderly parent"),
    BarrierType.FACILITY_ACCESS: ("queue", "closed", "no bed", "long wait", "waiting time", "documents", "referral slip"),
}
_BARRIER_ACTION = {
    BarrierType.FINANCIAL: "Check eligibility for referral-cost support schemes and share scheme information.",
    BarrierType.TRANSPORTATION: "Explore transport options or community transport support for the visit.",
    BarrierType.FEAR: "Arrange a supportive conversation with the health worker about the logistics of the visit.",
    BarrierType.CONFUSION: "Re-share facility name, address, timing and documents to bring, in the family's language.",
    BarrierType.FAMILY_SOCIAL: "Discuss accompaniment or caregiving arrangements with the family.",
    BarrierType.FACILITY_ACCESS: "Contact the receiving facility to pre-arrange registration and reduce waiting.",
    BarrierType.OTHER: "Health worker to review the case and identify the logistical obstacle.",
    BarrierType.UNKNOWN: "Health worker to contact the family and ask about obstacles to attending.",
}
_BARRIER_TEMPLATE = {
    BarrierType.FINANCIAL: "TPL_FINANCIAL_SUPPORT_INFO",
    BarrierType.TRANSPORTATION: "TPL_TRANSPORT_INFO",
    BarrierType.FEAR: "TPL_REASSURANCE_SUPPORT",
    BarrierType.CONFUSION: "TPL_CLARIFY_INSTRUCTIONS",
}
_BASE_DELAY = {UrgencyLevel.EMERGENCY: 1, UrgencyLevel.URGENT: 6, UrgencyLevel.SEMI_URGENT: 24, UrgencyLevel.ROUTINE: 48}
_CHANNEL_ROTATION = [ContactChannel.SMS, ContactChannel.PHONE_CALL, ContactChannel.WHATSAPP]
_FAILED_OUTCOMES = {ContactOutcome.NO_ANSWER, ContactOutcome.NUMBER_UNREACHABLE, ContactOutcome.MESSAGE_FAILED}


class MockAIAgentProvider(AIAgentProvider):
    def interpret_barrier(self, request: BarrierInterpretationRequest) -> BarrierInterpretationResponse:
        text = request.referral_note.lower()
        hits: dict[BarrierType, list[str]] = {}
        for btype, words in _KEYWORDS.items():
            found = [w for w in words if w in text]
            if found:
                hits[btype] = found
        if not hits:
            return BarrierInterpretationResponse(
                barrier_type=BarrierType.UNKNOWN, extracted_evidence=[],
                explanation="No logistical barrier could be identified from the referral note.",
                confidence=0.3, uncertainty_flag=True,
                recommended_logistical_action=_BARRIER_ACTION[BarrierType.UNKNOWN])
        best = max(hits, key=lambda b: len(hits[b]))
        confidence = min(0.95, 0.5 + 0.15 * len(hits[best]))
        if len(hits) > 1:
            confidence = max(0.3, confidence - 0.15)
        return BarrierInterpretationResponse(
            barrier_type=best, extracted_evidence=hits[best],
            explanation=f"Referral note mentions terms associated with a {best.value.replace('_', ' ')} barrier.",
            confidence=round(confidence, 2), uncertainty_flag=len(hits) > 1,
            recommended_logistical_action=_BARRIER_ACTION[best])

    def recommend_contact_strategy(self, request: ContactStrategyRequest) -> ContactStrategyResponse:
        prefs = request.family_communication_preferences
        blocked = set(prefs.do_not_contact_channels)
        attempts = sorted(request.previous_attempts, key=lambda a: a.attempt_number)
        channel = prefs.preferred_channel or ContactChannel.SMS
        if attempts and attempts[-1].contact_outcome in _FAILED_OUTCOMES:
            idx = _CHANNEL_ROTATION.index(attempts[-1].channel)
            channel = _CHANNEL_ROTATION[(idx + 1) % len(_CHANNEL_ROTATION)]
        for _ in range(len(_CHANNEL_ROTATION)):
            if channel not in blocked:
                break
            channel = _CHANNEL_ROTATION[(_CHANNEL_ROTATION.index(channel) + 1) % len(_CHANNEL_ROTATION)]
        urgency = request.referral_context.human_assigned_urgency
        delay = float(_BASE_DELAY[urgency])
        failures = sum(1 for a in attempts if a.contact_outcome in _FAILED_OUTCOMES)
        if failures >= 2:
            delay = max(1.0, delay / 2)
        template = "TPL_REQUEST_CALLBACK" if failures else _BARRIER_TEMPLATE.get(
            request.barrier_type, "TPL_REFERRAL_REMINDER")
        confidence = 0.8 if failures < 3 else 0.5
        return ContactStrategyResponse(
            recommended_channel=channel, recommended_delay_hours=delay,
            preferred_time_of_day=prefs.preferred_time_of_day or TimeOfDay.ANY,
            message_template_id=template,
            reasoning=f"{failures} failed attempt(s) so far; chose {channel.value} respecting family preferences.",
            confidence=confidence, requires_human_review=failures >= 3)

    def synthesize_escalation(self, request: EscalationSynthesisRequest) -> EscalationSynthesisResponse:
        ctx = request.referral_context
        barrier = request.barrier_interpretation.barrier_type if request.barrier_interpretation else None
        failed = [a for a in request.followup_history if a.contact_outcome in _FAILED_OUTCOMES]
        overdue = ctx.hours_until_deadline is not None and ctx.hours_until_deadline <= 0
        if barrier == BarrierType.TRANSPORTATION:
            action = EscalationAction.ARRANGE_TRANSPORT_SUPPORT
        elif barrier == BarrierType.FINANCIAL:
            action = EscalationAction.REQUEST_FINANCIAL_ASSISTANCE
        elif len(failed) >= 3:
            action = EscalationAction.HOME_VISIT_REVIEW
        elif barrier == BarrierType.FACILITY_ACCESS:
            action = EscalationAction.FACILITY_DIRECT_CONTACT
        else:
            action = EscalationAction.SUPERVISOR_REVIEW
        urgency = ctx.human_assigned_urgency
        if overdue and urgency in (UrgencyLevel.ROUTINE, UrgencyLevel.SEMI_URGENT):
            urgency = UrgencyLevel.URGENT  # may raise, never lower
        reasons = []
        if overdue:
            reasons.append("referral deadline has passed without confirmed arrival")
        if failed:
            reasons.append(f"{len(failed)} unsuccessful contact attempt(s)")
        if barrier and barrier != BarrierType.UNKNOWN:
            reasons.append(f"identified {barrier.value.replace('_', ' ')} barrier")
        reason = "; ".join(reasons) or "case flagged for supervisory review"
        evidence = [f"{e.event_type}: {e.description}" for e in request.case_events[-5:]]
        evidence += [f"attempt {a.attempt_number} via {a.channel.value}: {a.contact_outcome.value}"
                     for a in request.followup_history[-5:]]
        summary = (f"Referral {ctx.referral_reference} (status {ctx.status.value}, human urgency "
                   f"{ctx.human_assigned_urgency.value}). {len(request.followup_history)} follow-up attempt(s) recorded. "
                   f"Barrier: {barrier.value if barrier else 'not assessed'}.")
        uncertain = barrier in (None, BarrierType.UNKNOWN) or (
            request.barrier_interpretation is not None
            and (request.barrier_interpretation.confidence or 0) < get_settings().ai_confidence_threshold)
        return EscalationSynthesisResponse(
            case_summary=summary, escalation_reason=reason, recommended_action=action,
            urgency_recommendation=urgency, supporting_evidence=evidence, uncertainty_flag=uncertain)


# --------------------------------------------------------------------------------------
# Gateway used by the rest of the backend
# --------------------------------------------------------------------------------------
class AIAgentService:
    def __init__(self, provider: AIAgentProvider, confidence_threshold: float | None = None):
        self.provider = provider
        self.threshold = (get_settings().ai_confidence_threshold
                          if confidence_threshold is None else confidence_threshold)

    def _call(self, fn, request: BaseModel, response_model: type[BaseModel]) -> Any:
        try:
            raw = fn(request)
        except AIAgentError:
            raise
        except Exception as exc:  # provider crashed / timed out
            logger.warning("AI provider failure in %s: %s", fn.__name__, type(exc).__name__)
            raise AIAgentError(f"AI provider failure: {type(exc).__name__}") from exc
        try:
            data = raw.model_dump() if isinstance(raw, BaseModel) else raw
            return response_model.model_validate(data)
        except (ValidationError, TypeError) as exc:
            raise InvalidAIResponseError(f"Invalid response from AI agent: {exc}") from exc

    def interpret_barrier(self, request: BarrierInterpretationRequest) -> BarrierInterpretationResponse:
        resp = self._call(self.provider.interpret_barrier, request, BarrierInterpretationResponse)
        if resp.confidence < self.threshold or resp.barrier_type == BarrierType.UNKNOWN:
            resp.uncertainty_flag = True  # uncertain output must go to human review
        return resp

    def recommend_contact_strategy(self, request: ContactStrategyRequest) -> ContactStrategyResponse:
        resp = self._call(self.provider.recommend_contact_strategy, request, ContactStrategyResponse)
        if resp.recommended_channel in request.family_communication_preferences.do_not_contact_channels:
            raise InvalidAIResponseError("AI recommended a channel the family asked not to be contacted on")
        if resp.confidence < self.threshold:
            resp.requires_human_review = True
        return resp

    def synthesize_escalation(self, request: EscalationSynthesisRequest) -> EscalationSynthesisResponse:
        return self._call(self.provider.synthesize_escalation, request, EscalationSynthesisResponse)


# --------------------------------------------------------------------------------------
# Provider wiring
# --------------------------------------------------------------------------------------
_service: AIAgentService | None = None


def load_provider(spec: str) -> AIAgentProvider:
    if spec == "mock":
        return MockAIAgentProvider()
    module_name, _, cls_name = spec.partition(":")
    if not module_name or not cls_name:
        raise ValueError("AI_AGENT_PROVIDER must be 'mock' or 'package.module:ClassName'")
    provider = getattr(importlib.import_module(module_name), cls_name)()
    if not isinstance(provider, AIAgentProvider):
        raise TypeError(f"{spec} does not implement AIAgentProvider")
    return provider


def get_ai_service() -> AIAgentService:
    global _service
    if _service is None:
        _service = AIAgentService(load_provider(get_settings().ai_agent_provider))
    return _service


def set_ai_service(service: AIAgentService | None) -> None:
    """Override the gateway (tests / alternative wiring). Pass None to reset."""
    global _service
    _service = service
