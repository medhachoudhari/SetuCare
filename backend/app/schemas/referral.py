from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.ai_agents import CommunicationPreferences
from app.models.enums import BarrierType, ReferralOutcome, ReferralStatus, UrgencyLevel
from app.schemas.common import StrictModel


class ReferralCreate(StrictModel):
    patient_identifier: str = Field(min_length=1, max_length=64, description="Pseudonymous patient ID, not a name")
    patient_name: str | None = Field(default=None, max_length=150)
    age: int | None = Field(default=None, ge=0, le=120)
    receiving_facility_id: int
    human_assigned_urgency: UrgencyLevel
    referral_note: str = Field(min_length=3, max_length=5000)
    communication_preferences: CommunicationPreferences | None = None
    # Administrators only (health workers' values for these are derived server-side)
    referring_facility_id: int | None = None
    assigned_health_worker_id: int | None = None


class ReferralUpdate(StrictModel):
    patient_name: str | None = Field(default=None, max_length=150)
    age: int | None = Field(default=None, ge=0, le=120)
    receiving_facility_id: int | None = None
    human_assigned_urgency: UrgencyLevel | None = None
    referral_note: str | None = Field(default=None, min_length=3, max_length=5000)
    communication_preferences: CommunicationPreferences | None = None


class StatusChange(StrictModel):
    status: ReferralStatus
    reason: str | None = Field(default=None, max_length=1000)
    outcome: ReferralOutcome | None = None
    outcome_notes: str | None = Field(default=None, max_length=2000)


class ArrivalConfirmation(StrictModel):
    arrival_time: datetime | None = Field(default=None, description="Defaults to now; must not be in the future")
    notes: str | None = Field(default=None, max_length=1000)


class ReferralOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    referral_reference: str
    patient_identifier: str
    patient_name: str | None = None  # populated only for authorised roles
    age: int | None
    referring_facility_id: int
    receiving_facility_id: int
    referring_facility_name: str | None = None
    receiving_facility_name: str | None = None
    assigned_health_worker_id: int | None
    human_assigned_urgency: UrgencyLevel
    referral_note: str
    communication_preferences: dict | None = None
    barrier_type: BarrierType | None = None
    barrier_reasoning: str | None = None
    barrier_confidence: float | None = None
    barrier_needs_review: bool = False
    status: ReferralStatus
    deadline: datetime
    is_overdue: bool = False
    outcome: ReferralOutcome | None = None
    outcome_notes: str | None = None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None


class StatusHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    from_status: ReferralStatus | None
    to_status: ReferralStatus
    changed_by: int | None
    reason: str | None
    changed_at: datetime


class ReferralHistoryOut(BaseModel):
    referral_id: int
    referral_reference: str
    status_history: list[StatusHistoryOut]
