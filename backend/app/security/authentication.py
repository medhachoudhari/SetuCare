"""Password hashing, JWT issue/verify, and current-user dependencies."""
import threading
import time
from datetime import timedelta

import bcrypt
import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db, utcnow
from app.models.user import User
from app.services.errors import ServiceError

bearer_scheme = HTTPBearer(auto_error=False, description="JWT from POST /api/auth/login")
_DUMMY_HASH = bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt(rounds=get_settings().bcrypt_rounds)).decode()


def hash_password(password: str) -> str:
    raw = password.encode()
    if len(raw) > 72:
        raise ServiceError(422, "password_too_long", "Password must be at most 72 bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=get_settings().bcrypt_rounds)).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], (hashed or _DUMMY_HASH).encode()) and hashed is not None
    except ValueError:
        return False


def create_access_token(user: User) -> tuple[str, int]:
    s = get_settings()
    expires = timedelta(minutes=s.access_token_expire_minutes)
    now = utcnow()
    # Only the user id is embedded. Role/facility are always re-read from the database.
    token = jwt.encode({"sub": str(user.id), "iat": now, "exp": now + expires},
                       s.secret_key, algorithm=s.jwt_algorithm)
    return token, int(expires.total_seconds())


def _unauthorized(message="Invalid or expired credentials") -> ServiceError:
    return ServiceError(401, "unauthorized", message)


def decode_token(token: str) -> int:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.secret_key, algorithms=[s.jwt_algorithm], options={"require": ["exp", "sub"]})
        return int(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise _unauthorized()


def get_current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                     db: Session = Depends(get_db)) -> User:
    if creds is None:
        raise _unauthorized("Authentication required")
    user = db.get(User, decode_token(creds.credentials))
    if user is None or not user.is_active:
        raise _unauthorized()
    return user


def get_optional_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                      db: Session = Depends(get_db)) -> User | None:
    return get_current_user(creds, db) if creds is not None else None


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.lower()))
    ok = verify_password(password, user.hashed_password if user else None)
    return user if (ok and user and user.is_active) else None


# ---- simple in-memory login throttle (per email). Use a shared store behind multiple workers. ----
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


def check_login_allowed(email: str) -> None:
    s = get_settings()
    now = time.monotonic()
    with _lock:
        recent = [t for t in _failures.get(email, []) if now - t < s.login_window_seconds]
        _failures[email] = recent
        if len(recent) >= s.login_max_attempts:
            raise ServiceError(429, "too_many_attempts", "Too many failed login attempts. Try again later.")


def record_login_failure(email: str) -> None:
    with _lock:
        _failures.setdefault(email, []).append(time.monotonic())


def clear_login_failures(email: str | None = None) -> None:
    with _lock:
        _failures.pop(email, None) if email else _failures.clear()
