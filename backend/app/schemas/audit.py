from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int | None
    referral_id: int | None
    action: str
    input_snapshot: dict | None
    output_snapshot: dict | None
    timestamp: datetime
