from datetime import datetime

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime, utcnow
from app.models.enums import FacilityType
from app.models.user import enum_col


class Facility(Base):
    __tablename__ = "facilities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    facility_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    facility_type: Mapped[FacilityType] = mapped_column(enum_col(FacilityType), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_information: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    users = relationship("User", back_populates="facility")
