"""Domain enumerations shared by models, schemas, services and the AI contract."""
from enum import Enum


class UserRole(str, Enum):
    HEALTH_WORKER = "health_worker"
    FACILITY_STAFF = "facility_staff"
    ADMINISTRATOR = "administrator"


class FacilityType(str, Enum):
    PHC = "primary_health_centre"
    CHC = "community_health_centre"
    DISTRICT_HOSPITAL = "district_hospital"
    TERTIARY_HOSPITAL = "tertiary_hospital"
    OTHER = "other"


class UrgencyLevel(str, Enum):
    ROUTINE = "routine"
    SEMI_URGENT = "semi_urgent"
    URGENT = "urgent"
    EMERGENCY = "emergency"


URGENCY_RANK = {
    UrgencyLevel.ROUTINE: 0,
    UrgencyLevel.SEMI_URGENT: 1,
    UrgencyLevel.URGENT: 2,
    UrgencyLevel.EMERGENCY: 3,
}


def max_urgency(a: UrgencyLevel, b: UrgencyLevel) -> UrgencyLevel:
    """Higher of two urgencies. Used so AI can never lower a human-assigned level."""
    return a if URGENCY_RANK[a] >= URGENCY_RANK[b] else b


class ReferralStatus(str, Enum):
    REFERRED = "referred"
    FOLLOW_UP_IN_PROGRESS = "follow_up_in_progress"
    ESCALATION_PENDING = "escalation_pending"
    ESCALATED = "escalated"
    CONFIRMED = "confirmed"  # facility staff confirmed patient arrival
    COMPLETED = "completed"  # facility staff recorded final outcome
    CANCELLED = "cancelled"
    LOST_TO_FOLLOW_UP = "lost_to_follow_up"


OPEN_STATUSES = (
    ReferralStatus.REFERRED,
    ReferralStatus.FOLLOW_UP_IN_PROGRESS,
    ReferralStatus.ESCALATION_PENDING,
    ReferralStatus.ESCALATED,
)
SUCCESS_STATUSES = (ReferralStatus.CONFIRMED, ReferralStatus.COMPLETED)
TERMINAL_STATUSES = (
    ReferralStatus.COMPLETED,
    ReferralStatus.CANCELLED,
    ReferralStatus.LOST_TO_FOLLOW_UP,
)


class ReferralOutcome(str, Enum):
    TREATED_AND_DISCHARGED = "treated_and_discharged"
    ADMITTED = "admitted"
    REFERRED_ONWARD = "referred_onward"
    DECLINED_CARE = "declined_care"


class BarrierType(str, Enum):
    FINANCIAL = "financial"
    TRANSPORTATION = "transportation"
    FEAR = "fear"
    CONFUSION = "confusion"
    FAMILY_SOCIAL = "family_social"
    FACILITY_ACCESS = "facility_access"
    OTHER = "other"
    UNKNOWN = "unknown"


class ContactChannel(str, Enum):
    SMS = "sms"
    PHONE_CALL = "phone_call"
    WHATSAPP = "whatsapp"


class TimeOfDay(str, Enum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    ANY = "any"


class ContactOutcome(str, Enum):
    PENDING = "pending"
    REACHED_WILL_ATTEND = "reached_will_attend"
    REACHED_NEEDS_SUPPORT = "reached_needs_support"
    REACHED_DECLINED = "reached_declined"
    NO_ANSWER = "no_answer"
    NUMBER_UNREACHABLE = "number_unreachable"
    MESSAGE_DELIVERED = "message_delivered"
    MESSAGE_FAILED = "message_failed"


class NextAction(str, Enum):
    AWAIT_FACILITY_CONFIRMATION = "await_facility_confirmation"
    SCHEDULE_NEXT_ATTEMPT = "schedule_next_attempt"
    RECOMMEND_ESCALATION = "recommend_escalation"
    VERIFY_CONTACT_DETAILS = "verify_contact_details"
    ARRANGE_SUPPORT_REVIEW = "arrange_support_review"


class EscalationStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"  # referral closed/confirmed before review


class EscalationAction(str, Enum):
    """Actions the AI may *recommend*. None is executed automatically."""

    SUPERVISOR_REVIEW = "supervisor_review"
    ARRANGE_TRANSPORT_SUPPORT = "arrange_transport_support"
    REQUEST_FINANCIAL_ASSISTANCE = "request_financial_assistance"
    FACILITY_DIRECT_CONTACT = "facility_direct_contact"
    HOME_VISIT_REVIEW = "home_visit_review"  # recommendation only; humans decide/dispatch
    COMMUNITY_OUTREACH_REVIEW = "community_outreach_review"
