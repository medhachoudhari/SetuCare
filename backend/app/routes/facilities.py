from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import UserRole
from app.models.facility import Facility
from app.models.user import User
from app.schemas.facility import FacilityCreate, FacilityOut, FacilityUpdate
from app.security.authentication import get_current_user
from app.security.permissions import require_roles
from app.services import audit_service
from app.services.errors import not_found

router = APIRouter(prefix="/api/facilities", tags=["facilities"])


@router.post("/", response_model=FacilityOut, status_code=201)
def create_facility(data: FacilityCreate, db: Session = Depends(get_db),
                    admin: User = Depends(require_roles(UserRole.ADMINISTRATOR))):
    facility = Facility(**data.model_dump())
    db.add(facility)
    db.flush()
    audit_service.log_action(db, action="facility.create", user_id=admin.id, output_snapshot={"facility_id": facility.id})
    db.commit()
    return facility


@router.get("/", response_model=list[FacilityOut])
def list_facilities(q: str | None = None, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    stmt = select(Facility).order_by(Facility.facility_name)
    if q:
        stmt = stmt.where(Facility.facility_name.ilike(f"%{q.strip()}%"))
    return list(db.scalars(stmt))


@router.get("/{facility_id}", response_model=FacilityOut)
def get_facility(facility_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    facility = db.get(Facility, facility_id)
    if facility is None:
        raise not_found("Facility")
    return facility


@router.patch("/{facility_id}", response_model=FacilityOut)
def update_facility(facility_id: int, data: FacilityUpdate, db: Session = Depends(get_db),
                    admin: User = Depends(require_roles(UserRole.ADMINISTRATOR))):
    facility = db.get(Facility, facility_id)
    if facility is None:
        raise not_found("Facility")
    changes = data.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(facility, k, v)
    audit_service.log_action(db, action="facility.update", user_id=admin.id,
                             output_snapshot={"facility_id": facility.id, **changes})
    db.commit()
    return facility
