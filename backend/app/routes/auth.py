from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.enums import UserRole
from app.models.facility import Facility
from app.models.user import User
from app.schemas.common import ErrorResponse, Page, make_page
from app.schemas.user import Token, UserAdminUpdate, UserLogin, UserOut, UserRegister
from app.security import authentication as auth
from app.security.permissions import require_roles
from app.services import audit_service
from app.services.errors import ServiceError, bad_request, conflict, forbidden, not_found

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201,
             responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def register(data: UserRegister, db: Session = Depends(get_db), caller: User | None = Depends(auth.get_optional_user)):
    """Public sign-up always creates an unassigned `health_worker`. Role and facility can only be
    chosen by an authenticated administrator; an administrator must otherwise assign them later."""
    is_admin = caller is not None and caller.role == UserRole.ADMINISTRATOR
    if not is_admin:
        if not get_settings().allow_public_registration:
            raise forbidden("Public registration is disabled")
        if data.facility_id is not None or (data.role not in (None, UserRole.HEALTH_WORKER)):
            raise forbidden("Only administrators can assign roles or facilities")
    role = data.role or UserRole.HEALTH_WORKER
    if data.facility_id is not None and db.get(Facility, data.facility_id) is None:
        raise bad_request("Facility does not exist", "validation_error")
    if role == UserRole.FACILITY_STAFF and data.facility_id is None:
        raise bad_request("facility_staff users require a facility_id", "validation_error")
    email = data.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise conflict("Email is already registered", "email_taken")
    user = User(full_name=data.full_name.strip(), email=email, hashed_password=auth.hash_password(data.password),
                role=role, facility_id=data.facility_id)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict("Email is already registered", "email_taken")
    audit_service.log_action(db, action="auth.register", user_id=caller.id if caller else user.id,
                             output_snapshot={"new_user_id": user.id, "role": user.role,
                                              "facility_id": user.facility_id})
    db.commit()
    return user


def _issue(db: Session, email: str, password: str) -> dict:
    email = email.lower()
    auth.check_login_allowed(email)
    user = auth.authenticate(db, email, password)
    if user is None:
        auth.record_login_failure(email)
        audit_service.log_action(db, action="auth.login_failed", input_snapshot={"email_hash_hint": email[:2] + "***"})
        db.commit()
        raise ServiceError(401, "invalid_credentials", "Invalid email or password")
    auth.clear_login_failures(email)
    token, ttl = auth.create_access_token(user)
    audit_service.log_action(db, action="auth.login", user_id=user.id)
    db.commit()
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl, "user": user}


@router.post("/login", response_model=Token, responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})
def login(data: UserLogin, db: Session = Depends(get_db)):
    return _issue(db, data.email, data.password)


@router.post("/token", response_model=Token, include_in_schema=True,
             summary="OAuth2 form login (used by the Swagger 'Authorize' button)")
def token_form(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    return _issue(db, form.username, form.password)


@router.get("/me", response_model=UserOut, responses={401: {"model": ErrorResponse}})
def me(user: User = Depends(auth.get_current_user)):
    return user


@router.get("/users", response_model=Page[UserOut], summary="List users (administrator)")
def list_users(page: int = 1, page_size: int = 50, db: Session = Depends(get_db),
               _: User = Depends(require_roles(UserRole.ADMINISTRATOR))):
    page, page_size = max(1, page), min(max(1, page_size), 200)
    from sqlalchemy import func
    total = db.scalar(select(func.count(User.id)))
    rows = db.scalars(select(User).order_by(User.id).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page(rows, total, page, page_size)


@router.patch("/users/{user_id}", response_model=UserOut, summary="Assign role/facility, activate/deactivate (administrator)")
def admin_update_user(user_id: int, data: UserAdminUpdate, db: Session = Depends(get_db),
                      admin: User = Depends(require_roles(UserRole.ADMINISTRATOR))):
    user = db.get(User, user_id)
    if user is None:
        raise not_found("User")
    changes = data.model_dump(exclude_unset=True)
    if "facility_id" in changes and changes["facility_id"] is not None and db.get(Facility, changes["facility_id"]) is None:
        raise bad_request("Facility does not exist", "validation_error")
    if user.id == admin.id and (changes.get("is_active") is False or
                                ("role" in changes and changes["role"] != UserRole.ADMINISTRATOR)):
        raise bad_request("Administrators cannot deactivate or demote themselves", "validation_error")
    before = {k: getattr(user, k) for k in changes}
    for k, v in changes.items():
        setattr(user, k, v)
    if user.role == UserRole.FACILITY_STAFF and user.facility_id is None:
        raise bad_request("facility_staff users require a facility_id", "validation_error")
    audit_service.log_action(db, action="auth.user_update", user_id=admin.id, input_snapshot=before,
                             output_snapshot={"user_id": user.id, **changes})
    db.commit()
    return user
