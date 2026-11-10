from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime, utcnow
from app.models.enums import EscalationStatus, UrgencyLevel
from app.models.user import enum_col


class Escalation(Base):
    __tablename__ = "escalations"
    __table_args__ = (Index("ix_escalations_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id", ondelete="CASCADE"), index=True, nullable=False)
    ai_generated_brief: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    urgency_level: Mapped[UrgencyLevel] = mapped_column(enum_col(UrgencyLevel), nullable=False)
    status: Mapped[EscalationStatus] = mapped_column(enum_col(EscalationStatus), nullable=False,
                                                     default=EscalationStatus.PENDING_REVIEW)
    # Immutable copy of the validated AI output (never edited after creation).
    ai_recommendation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    uncertainty_flag: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)  # None = scheduler
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    referral = relationship("Referral", back_populates="escalations")
    reviewer = relationship("User", foreign_keys=[reviewed_by])
