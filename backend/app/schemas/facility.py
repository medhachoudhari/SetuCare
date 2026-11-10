from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import FacilityType
from app.schemas.common import StrictModel


class FacilityCreate(StrictModel):
    facility_name: str = Field(min_length=2, max_length=200)
    facility_type: FacilityType
    location: str = Field(min_length=2, max_length=255)
    contact_information: str | None = Field(default=None, max_length=1000)


class FacilityUpdate(StrictModel):
    facility_name: str | None = Field(default=None, min_length=2, max_length=200)
    facility_type: FacilityType | None = None
    location: str | None = Field(default=None, min_length=2, max_length=255)
    contact_information: str | None = Field(default=None, max_length=1000)


class FacilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    facility_name: str
    facility_type: FacilityType
    location: str
    contact_information: str | None
    created_at: datetime
