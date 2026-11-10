from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime, utcnow
from app.models.enums import ContactChannel, ContactOutcome, NextAction
from app.models.user import enum_col


class FollowUp(Base):
    __tablename__ = "followups"
    __table_args__ = (
        UniqueConstraint("referral_id", "attempt_number", name="uq_followup_referral_attempt"),
        Index("ix_followups_due", "attempted_at", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id", ondelete="CASCADE"), index=True, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[ContactChannel] = mapped_column(enum_col(ContactChannel), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    attempted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    contact_outcome: Mapped[ContactOutcome] = mapped_column(enum_col(ContactOutcome), nullable=False,
                                                            default=ContactOutcome.PENDING)
    conversation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_recommended_action: Mapped[NextAction | None] = mapped_column(enum_col(NextAction), nullable=True)
    message_template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_recommendation: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # original AI output, preserved
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    referral = relationship("Referral", back_populates="followups")
