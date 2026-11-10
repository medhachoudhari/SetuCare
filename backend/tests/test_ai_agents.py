import pytest

from app.database import utcnow
from app.integrations.ai_agents import (
    APPROVED_TEMPLATES,
    AIAgentError,
    AIAgentProvider,
    AIAgentService,
    BarrierInterpretationRequest,
    BarrierInterpretationResponse,
    CommunicationPreferences,
    ContactStrategyRequest,
    EscalationSynthesisRequest,
    InvalidAIResponseError,
    MockAIAgentProvider,
    ReferralContext,
    load_provider,
    set_ai_service,
)
from app.models.enums import ContactChannel, ReferralStatus, UrgencyLevel


def ctx(urgency=UrgencyLevel.URGENT):
    return ReferralContext(referral_reference="SC-TEST-0001", status=ReferralStatus.REFERRED,
                           human_assigned_urgency=urgency, deadline=utcnow(), hours_until_deadline=5)


class ScriptedProvider(MockAIAgentProvider):
    """Mock whose outputs can be overridden per agent to simulate misbehaving AI."""

    def __init__(self, barrier=None, strategy=None, escalation=None, raises=None):
        self.barrier, self.strategy, self.escalation, self.raises = barrier, strategy, escalation, raises

    def interpret_barrier(self, request):
        if self.raises:
            raise self.raises
        return self.barrier if self.barrier is not None else super().interpret_barrier(request)

    def recommend_contact_strategy(self, request):
        return self.strategy if self.strategy is not None else super().recommend_contact_strategy(request)

    def synthesize_escalation(self, request):
        return self.escalation if self.escalation is not None else super().synthesize_escalation(request)


def test_mock_barrier_agent_outputs_valid_contract():
    svc = AIAgentService(MockAIAgentProvider())
    out = svc.interpret_barrier(BarrierInterpretationRequest(
        referral_note="They cannot afford the cost and the fee is too expensive",
        human_assigned_urgency=UrgencyLevel.URGENT, referral_context=ctx()))
    assert out.barrier_type.value == "financial" and 0 <= out.confidence <= 1
    assert out.extracted_evidence and out.recommended_logistical_action


def test_unknown_barrier_flagged_uncertain():
    out = AIAgentService(MockAIAgentProvider()).interpret_barrier(BarrierInterpretationRequest(
        referral_note="Routine review requested", human_assigned_urgency=UrgencyLevel.ROUTINE, referral_context=ctx()))
    assert out.barrier_type.value == "unknown" and out.uncertainty_flag is True


def test_low_confidence_is_forced_to_human_review():
    bad = BarrierInterpretationResponse(barrier_type="financial", extracted_evidence=[], explanation="x",
                                        confidence=0.2, uncertainty_flag=False, recommended_logistical_action="y")
    out = AIAgentService(ScriptedProvider(barrier=bad)).interpret_barrier(BarrierInterpretationRequest(
        referral_note="money", human_assigned_urgency=UrgencyLevel.URGENT, referral_context=ctx()))
    assert out.uncertainty_flag is True


@pytest.mark.parametrize("payload", [
    {"barrier_type": "alien_abduction", "explanation": "x", "confidence": 0.9, "uncertainty_flag": False,
     "recommended_logistical_action": "y", "extracted_evidence": []},
    {"barrier_type": "financial", "explanation": "x", "confidence": 1.7, "uncertainty_flag": False,
     "recommended_logistical_action": "y"},
    {"barrier_type": "financial", "explanation": "x", "confidence": 0.9},
    {"barrier_type": "financial", "explanation": "x", "confidence": 0.9, "uncertainty_flag": False,
     "recommended_logistical_action": "y", "human_assigned_urgency": "routine"},  # extra field: urgency tampering
    "not even a dict",
])
def test_invalid_barrier_responses_rejected(payload):
    svc = AIAgentService(ScriptedProvider(barrier=payload))
    with pytest.raises(InvalidAIResponseError):
        svc.interpret_barrier(BarrierInterpretationRequest(
            referral_note="money", human_assigned_urgency=UrgencyLevel.URGENT, referral_context=ctx()))


def test_provider_exception_wrapped():
    svc = AIAgentService(ScriptedProvider(raises=RuntimeError("boom")))
    with pytest.raises(AIAgentError):
        svc.interpret_barrier(BarrierInterpretationRequest(
            referral_note="money", human_assigned_urgency=UrgencyLevel.URGENT, referral_context=ctx()))


def test_unapproved_template_rejected():
    bad = {"recommended_channel": "sms", "recommended_delay_hours": 2, "message_template_id": "FREE_TEXT_MEDICAL_ADVICE",
           "reasoning": "r", "confidence": 0.9, "requires_human_review": False}
    with pytest.raises(InvalidAIResponseError):
        AIAgentService(ScriptedProvider(strategy=bad)).recommend_contact_strategy(
            ContactStrategyRequest(referral_context=ctx()))


def test_do_not_contact_channel_respected_by_mock_and_enforced_on_provider():
    prefs = CommunicationPreferences(do_not_contact_channels=[ContactChannel.SMS, ContactChannel.WHATSAPP])
    req = ContactStrategyRequest(referral_context=ctx(), family_communication_preferences=prefs)
    assert AIAgentService(MockAIAgentProvider()).recommend_contact_strategy(req).recommended_channel == ContactChannel.PHONE_CALL
    bad = {"recommended_channel": "sms", "recommended_delay_hours": 2, "message_template_id": "TPL_REFERRAL_REMINDER",
           "reasoning": "r", "confidence": 0.9, "requires_human_review": False}
    with pytest.raises(InvalidAIResponseError):
        AIAgentService(ScriptedProvider(strategy=bad)).recommend_contact_strategy(req)


def test_mock_strategy_templates_are_all_approved():
    svc = AIAgentService(MockAIAgentProvider())
    out = svc.recommend_contact_strategy(ContactStrategyRequest(referral_context=ctx(), barrier_type="transportation"))
    assert out.message_template_id in APPROVED_TEMPLATES


def test_escalation_synthesis_mock_contract():
    out = AIAgentService(MockAIAgentProvider()).synthesize_escalation(
        EscalationSynthesisRequest(referral_context=ctx(), referral_note="needs help"))
    assert out.case_summary and out.escalation_reason and out.recommended_action


def test_load_provider():
    assert isinstance(load_provider("mock"), MockAIAgentProvider)
    with pytest.raises(ValueError):
        load_provider("nonsense")
    assert issubclass(type(load_provider("tests.test_ai_agents:ScriptedProvider")), AIAgentProvider)


def test_referral_creation_survives_ai_outage(client, world):
    from tests.conftest import referral_payload
    set_ai_service(AIAgentService(ScriptedProvider(raises=RuntimeError("down"))))
    r = client.post("/api/referrals/", json=referral_payload(world), headers=world.h["hw1"])
    assert r.status_code == 201
    body = r.json()
    assert body["barrier_type"] == "unknown" and body["barrier_needs_review"] is True


def test_invalid_ai_output_never_alters_referral(client, world, db):
    from tests.conftest import referral_payload
    bad = {"barrier_type": "financial", "explanation": "x", "confidence": 0.9, "uncertainty_flag": False,
           "recommended_logistical_action": "y", "human_assigned_urgency": "routine"}
    set_ai_service(AIAgentService(ScriptedProvider(barrier=bad)))
    body = client.post("/api/referrals/", json=referral_payload(world, human_assigned_urgency="emergency"),
                       headers=world.h["hw1"]).json()
    assert body["human_assigned_urgency"] == "emergency" and body["barrier_needs_review"] is True
