from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime, utcnow
from app.models.enums import BarrierType, ReferralOutcome, ReferralStatus, UrgencyLevel
from app.models.user import enum_col


class Referral(Base):
    __tablename__ = "referrals"
    __table_args__ = (
        CheckConstraint("referring_facility_id <> receiving_facility_id", name="ck_referral_distinct_facilities"),
        CheckConstraint("barrier_confidence IS NULL OR (barrier_confidence >= 0 AND barrier_confidence <= 1)",
                        name="ck_referral_confidence_range"),
        CheckConstraint("age IS NULL OR (age >= 0 AND age <= 120)", name="ck_referral_age_range"),
        Index("ix_referrals_status_deadline", "status", "deadline"),
        Index("ix_referrals_receiving_status", "receiving_facility_id", "status"),
        Index("ix_referrals_worker_status", "assigned_health_worker_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_reference: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    patient_identifier: Mapped[str] = mapped_column(String(64), index=True, nullable=False)  # pseudonymous ID
    patient_name: Mapped[str | None] = mapped_column(String(150), nullable=True)  # access-controlled
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    referring_facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), nullable=False)
    receiving_facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), nullable=False)
    assigned_health_worker_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    human_assigned_urgency: Mapped[UrgencyLevel] = mapped_column(enum_col(UrgencyLevel), nullable=False)
    referral_note: Mapped[str] = mapped_column(Text, nullable=False)
    communication_preferences: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    barrier_type: Mapped[BarrierType | None] = mapped_column(enum_col(BarrierType), nullable=True)
    barrier_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    barrier_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    barrier_needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[ReferralStatus] = mapped_column(enum_col(ReferralStatus), nullable=False,
                                                   default=ReferralStatus.REFERRED, index=True)
    deadline: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    outcome: Mapped[ReferralOutcome | None] = mapped_column(enum_col(ReferralOutcome), nullable=True)
    outcome_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    referring_facility = relationship("Facility", foreign_keys=[referring_facility_id])
    receiving_facility = relationship("Facility", foreign_keys=[receiving_facility_id])
    assigned_health_worker = relationship("User", foreign_keys=[assigned_health_worker_id])
    followups = relationship("FollowUp", back_populates="referral", cascade="all, delete-orphan",
                             order_by="FollowUp.attempt_number")
    escalations = relationship("Escalation", back_populates="referral", cascade="all, delete-orphan")
    status_history = relationship("ReferralStatusHistory", back_populates="referral", cascade="all, delete-orphan",
                                  order_by="ReferralStatusHistory.id")


class ReferralStatusHistory(Base):
    __tablename__ = "referral_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id", ondelete="CASCADE"), index=True, nullable=False)
    from_status: Mapped[ReferralStatus | None] = mapped_column(enum_col(ReferralStatus), nullable=True)
    to_status: Mapped[ReferralStatus] = mapped_column(enum_col(ReferralStatus), nullable=False)
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)  # None = system
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    referral = relationship("Referral", back_populates="status_history")
