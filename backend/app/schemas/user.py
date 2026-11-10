import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import UserRole
from app.schemas.common import StrictModel


def _check_password(v: str) -> str:
    if len(v.encode()) > 72:
        raise ValueError("password must be at most 72 bytes")
    if not (re.search(r"[A-Za-z]", v) and re.search(r"\d", v)):
        raise ValueError("password must contain letters and digits")
    return v


class UserRegister(StrictModel):
    full_name: str = Field(min_length=2, max_length=150)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    # Honoured ONLY when the caller is an authenticated administrator.
    role: UserRole | None = None
    facility_id: int | None = None

    _pw = field_validator("password")(_check_password)


class UserLogin(StrictModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class UserAdminUpdate(StrictModel):
    role: UserRole | None = None
    facility_id: int | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    full_name: str
    email: EmailStr
    role: UserRole
    facility_id: int | None
    is_active: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
