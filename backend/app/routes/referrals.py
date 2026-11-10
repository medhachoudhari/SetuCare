from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.integrations.ai_agents import AIAgentService, get_ai_service
from app.models.enums import BarrierType, ReferralStatus, UrgencyLevel, UserRole
from app.models.user import User
from app.schemas.common import ErrorResponse, Page, make_page
from app.schemas.referral import (
    ArrivalConfirmation,
    ReferralCreate,
    ReferralHistoryOut,
    ReferralOut,
    ReferralUpdate,
    StatusChange,
)
from app.security.authentication import get_current_user
from app.services import audit_service, referral_service

router = APIRouter(prefix="/api/referrals", tags=["referrals"],
                   responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})


@router.post("/", response_model=ReferralOut, status_code=201, responses={403: {"model": ErrorResponse}})
def create_referral(data: ReferralCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user),
                    ai: AIAgentService = Depends(get_ai_service)):
    referral = referral_service.create_referral(db, user, data, ai)
    return referral_service.serialize(referral, user)


@router.get("/", response_model=Page[ReferralOut])
def list_referrals(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    status: ReferralStatus | None = None, urgency: UrgencyLevel | None = None,
    barrier_type: BarrierType | None = None, overdue: bool | None = None,
    q: str | None = Query(None, max_length=64, description="Search reference or patient identifier"),
    reference: str | None = Query(None, max_length=32, description="Exact referral reference"),
    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows, total = referral_service.list_referrals(db, user, page=page, page_size=page_size, status=status,
                                                  urgency=urgency, barrier_type=barrier_type, overdue=overdue,
                                                  q=q, reference=reference)
    return make_page([referral_service.serialize(r, user) for r in rows], total, page, page_size)


@router.get("/{referral_id}", response_model=ReferralOut)
def get_referral(referral_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    referral = referral_service.get_for_user(db, user, referral_id)
    audit_service.log_action(db, action="referral.view", user_id=user.id, referral_id=referral.id)
    db.commit()
    return referral_service.serialize(referral, user)


@router.patch("/{referral_id}", response_model=ReferralOut, responses={403: {"model": ErrorResponse}})
def update_referral(referral_id: int, data: ReferralUpdate, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user), ai: AIAgentService = Depends(get_ai_service)):
    referral = referral_service.get_for_user(db, user, referral_id)
    referral = referral_service.update_referral(db, user, referral, data, ai)
    return referral_service.serialize(referral, user)


@router.patch("/{referral_id}/status", response_model=ReferralOut,
              responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def change_status(referral_id: int, data: StatusChange, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    referral = referral_service.get_for_user(db, user, referral_id)
    referral = referral_service.change_status(db, user, referral, data)
    return referral_service.serialize(referral, user)


@router.post("/{referral_id}/confirm-arrival", response_model=ReferralOut,
             summary="Facility staff: confirm patient arrival",
             responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def confirm_arrival(referral_id: int, data: ArrivalConfirmation | None = None, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    referral = referral_service.get_for_user(db, user, referral_id)
    referral = referral_service.confirm_arrival(db, user, referral, data or ArrivalConfirmation())
    return referral_service.serialize(referral, user)


@router.post("/{referral_id}/analyze-barrier", response_model=ReferralOut,
             summary="Re-run Agent 1 (barrier interpretation)")
def analyze_barrier(referral_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
                    ai: AIAgentService = Depends(get_ai_service)):
    from app.services.errors import forbidden
    if user.role == UserRole.FACILITY_STAFF:
        raise forbidden("Facility staff cannot run barrier analysis")
    referral = referral_service.get_for_user(db, user, referral_id)
    referral_service.run_barrier_analysis(db, referral, ai, user)
    db.commit()
    return referral_service.serialize(referral, user)


@router.get("/{referral_id}/history", response_model=ReferralHistoryOut)
def referral_history(referral_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    referral = referral_service.get_for_user(db, user, referral_id)
    return {"referral_id": referral.id, "referral_reference": referral.referral_reference,
            "status_history": referral.status_history}
