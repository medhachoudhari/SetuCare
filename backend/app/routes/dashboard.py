from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.common import ErrorResponse
from app.schemas.dashboard import DashboardSummary, ReferralListItem, ReferralStatistics
from app.security.authentication import get_current_user
from app.services import dashboard_service

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"], responses={401: {"model": ErrorResponse}})


@router.get("/summary", response_model=DashboardSummary)
def summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return dashboard_service.summary(db, user)


@router.get("/referral-statistics", response_model=ReferralStatistics)
def referral_statistics(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return dashboard_service.statistics(db, user)


@router.get("/urgent-referrals", response_model=list[ReferralListItem])
def urgent_referrals(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    return dashboard_service.urgent_referrals(db, user, limit)


@router.get("/pending-confirmations", response_model=list[ReferralListItem])
def pending_confirmations(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    return dashboard_service.pending_confirmations(db, user, limit)
