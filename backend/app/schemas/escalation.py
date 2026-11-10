from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import EscalationStatus, UrgencyLevel
from app.schemas.common import StrictModel


class EscalationCreate(StrictModel):
    referral_id: int


class EscalationReview(StrictModel):
    reviewer_notes: str | None = Field(default=None, max_length=2000)


class EscalationReject(StrictModel):
    reviewer_notes: str = Field(min_length=3, max_length=2000, description="Reason for rejection")


class EscalationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    referral_id: int
    ai_generated_brief: str
    reason: str
    recommended_action: str
    urgency_level: UrgencyLevel
    status: EscalationStatus
    uncertainty_flag: bool
    ai_recommendation: dict | None
    created_by: int | None
    reviewed_by: int | None
    reviewed_at: datetime | None
    reviewer_notes: str | None
    created_at: datetime
