from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.integrations.ai_agents import AIAgentService, get_ai_service
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.common import ErrorResponse
from app.schemas.escalation import EscalationCreate, EscalationOut, EscalationReject, EscalationReview
from app.security.authentication import get_current_user
from app.security.permissions import require_roles
from app.services import escalation_service, referral_service

router = APIRouter(prefix="/api/escalations", tags=["escalations"],
                   responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse},
                              404: {"model": ErrorResponse}})
_humans = require_roles(UserRole.HEALTH_WORKER, UserRole.ADMINISTRATOR)


@router.post("/", response_model=EscalationOut, status_code=201, responses={409: {"model": ErrorResponse}},
             summary="Request an AI-synthesised escalation recommendation (starts as pending_review)")
def create_escalation(data: EscalationCreate, db: Session = Depends(get_db), user: User = Depends(_humans),
                      ai: AIAgentService = Depends(get_ai_service)):
    referral = referral_service.get_for_user(db, user, data.referral_id)
    esc = escalation_service.create_escalation(db, referral, user, ai)
    db.commit()
    return esc


@router.get("/pending", response_model=list[EscalationOut])
def pending_escalations(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return escalation_service.list_pending(db, user)


@router.get("/{escalation_id}", response_model=EscalationOut)
def get_escalation(escalation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return escalation_service.get_for_user(db, user, escalation_id)


@router.post("/{escalation_id}/approve", response_model=EscalationOut, responses={409: {"model": ErrorResponse}})
def approve_escalation(escalation_id: int, data: EscalationReview | None = None, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    esc = escalation_service.get_for_user(db, user, escalation_id)
    return escalation_service.approve(db, user, esc, data.reviewer_notes if data else None)


@router.post("/{escalation_id}/reject", response_model=EscalationOut, responses={409: {"model": ErrorResponse}})
def reject_escalation(escalation_id: int, data: EscalationReject, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    esc = escalation_service.get_for_user(db, user, escalation_id)
    return escalation_service.reject(db, user, esc, data.reviewer_notes)
