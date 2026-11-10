from pydantic import BaseModel

from app.models.enums import ReferralStatus, UrgencyLevel
from app.schemas.referral import ReferralOut


class FollowUpStats(BaseModel):
    total: int
    attempted: int
    pending_due: int
    pending_upcoming: int
    successful_contacts: int
    contact_success_rate: float


class DashboardSummary(BaseModel):
    total_referrals: int
    pending_referrals: int
    confirmed_referrals: int
    overdue_referrals: int
    pending_escalations: int
    referral_confirmation_rate: float
    followups: FollowUpStats


class ReferralStatistics(BaseModel):
    by_status: dict[str, int]
    by_urgency: dict[str, int]
    by_barrier_type: dict[str, int]
    followups: FollowUpStats
    average_hours_to_confirmation: float | None


class ReferralListItem(BaseModel):
    id: int
    referral_reference: str
    patient_identifier: str
    status: ReferralStatus
    human_assigned_urgency: UrgencyLevel
    deadline: str
    is_overdue: bool
    hours_remaining: float
    receiving_facility_id: int
    receiving_facility_name: str | None = None
