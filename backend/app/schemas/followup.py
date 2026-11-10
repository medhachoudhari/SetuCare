from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ContactChannel, ContactOutcome, NextAction
from app.schemas.common import StrictModel


class FollowUpCreate(StrictModel):
    referral_id: int
    channel: ContactChannel | None = Field(default=None, description="Omit to use the AI contact strategy")
    scheduled_at: datetime | None = Field(default=None, description="Omit for deterministic policy scheduling")


class FollowUpUpdate(StrictModel):
    attempted_at: datetime | None = None
    contact_outcome: ContactOutcome | None = None
    conversation_summary: str | None = Field(default=None, max_length=2000)
    scheduled_at: datetime | None = None
    channel: ContactChannel | None = None


class FollowUpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    referral_id: int
    attempt_number: int
    channel: ContactChannel
    scheduled_at: datetime
    attempted_at: datetime | None
    contact_outcome: ContactOutcome
    conversation_summary: str | None
    next_recommended_action: NextAction | None
    message_template_id: str | None
    message_preview: str | None = None
    ai_recommendation: dict | None
    requires_human_review: bool
    created_at: datetime


class FollowUpUpdateResult(BaseModel):
    followup: FollowUpOut
    next_followup: FollowUpOut | None = None
