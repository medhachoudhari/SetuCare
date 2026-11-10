from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.integrations.ai_agents import AIAgentService, get_ai_service
from app.models.user import User
from app.schemas.common import ErrorResponse
from app.schemas.followup import FollowUpCreate, FollowUpOut, FollowUpUpdate, FollowUpUpdateResult
from app.security.authentication import get_current_user
from app.services import followup_service, referral_service, scheduling_service

router = APIRouter(prefix="/api/followups", tags=["followups"],
                   responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse},
                              404: {"model": ErrorResponse}})


@router.post("/", response_model=FollowUpOut, status_code=201, responses={409: {"model": ErrorResponse}})
def create_followup(data: FollowUpCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user),
                    ai: AIAgentService = Depends(get_ai_service)):
    return followup_service.serialize(followup_service.create_followup(db, user, data, ai))


@router.get("/pending", response_model=list[FollowUpOut])
def pending_followups(include_upcoming: bool = Query(False, description="Also include not-yet-due follow-ups"),
                      db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    followup_service.require_followup_role(user)
    return [followup_service.serialize(f)
            for f in scheduling_service.find_pending_followups(db, user, include_upcoming=include_upcoming)]


@router.get("/referral/{referral_id}", response_model=list[FollowUpOut])
def followups_for_referral(referral_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    followup_service.require_followup_role(user)
    referral = referral_service.get_for_user(db, user, referral_id)
    return [followup_service.serialize(f) for f in referral.followups]


@router.patch("/{followup_id}", response_model=FollowUpUpdateResult, responses={409: {"model": ErrorResponse}},
              summary="Record contact attempt/outcome; schedules the next attempt deterministically")
def update_followup(followup_id: int, data: FollowUpUpdate, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user), ai: AIAgentService = Depends(get_ai_service)):
    fu = followup_service.get_followup_for_user(db, user, followup_id)
    fu, nxt = followup_service.update_followup(db, user, fu, data, ai)
    return {"followup": followup_service.serialize(fu), "next_followup": followup_service.serialize(nxt) if nxt else None}
