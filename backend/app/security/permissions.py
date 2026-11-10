"""Role-based access control and per-record scoping."""
from fastapi import Depends
from sqlalchemy import Select, false

from app.models.enums import UserRole
from app.models.referral import Referral
from app.models.user import User
from app.security.authentication import get_current_user
from app.services.errors import forbidden


def require_roles(*roles: UserRole):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise forbidden("Your role is not permitted to perform this action")
        return user

    return dependency


def can_access_referral(user: User, referral: Referral) -> bool:
    if user.role == UserRole.ADMINISTRATOR:
        return True
    if user.role == UserRole.HEALTH_WORKER:
        return referral.assigned_health_worker_id == user.id
    if user.role == UserRole.FACILITY_STAFF:
        return user.facility_id is not None and referral.receiving_facility_id == user.facility_id
    return False


def scope_referrals(stmt: Select, user: User) -> Select:
    """Restrict a SELECT on Referral to rows the user may see."""
    if user.role == UserRole.ADMINISTRATOR:
        return stmt
    if user.role == UserRole.HEALTH_WORKER:
        return stmt.where(Referral.assigned_health_worker_id == user.id)
    if user.role == UserRole.FACILITY_STAFF and user.facility_id is not None:
        return stmt.where(Referral.receiving_facility_id == user.facility_id)
    return stmt.where(false())


def can_view_patient_name(user: User, referral: Referral) -> bool:
    """Patient names are limited to the assigned health worker and administrators."""
    return user.role == UserRole.ADMINISTRATOR or (
        user.role == UserRole.HEALTH_WORKER and referral.assigned_health_worker_id == user.id)


def can_review_escalation(user: User, referral: Referral) -> bool:
    """Humans only: administrators, or the health worker responsible for the referral."""
    return user.role == UserRole.ADMINISTRATOR or (
        user.role == UserRole.HEALTH_WORKER and referral.assigned_health_worker_id == user.id)
