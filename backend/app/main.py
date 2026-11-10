import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import models  # noqa: F401  (register tables)
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.routes import audit, auth, dashboard, escalations, facilities, followups, referrals
from app.services.errors import ServiceError

logger = logging.getLogger("setucare")


def _seed_bootstrap_admin() -> None:
    from app.models.enums import UserRole
    from app.models.user import User
    from app.security.authentication import hash_password

    s = get_settings()
    if not (s.bootstrap_admin_email and s.bootstrap_admin_password):
        return
    with SessionLocal() as db:
        email = s.bootstrap_admin_email.lower()
        if not db.scalar(select(User.id).where(User.email == email)):
            db.add(User(full_name="Bootstrap Administrator", email=email,
                        hashed_password=hash_password(s.bootstrap_admin_password), role=UserRole.ADMINISTRATOR))
            db.commit()
            logger.info("Bootstrap administrator created")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if s.auto_create_tables:
        Base.metadata.create_all(engine)
    _seed_bootstrap_admin()
    scheduler = None
    if s.enable_scheduler:
        from apscheduler.schedulers.background import BackgroundScheduler

        from app.services.scheduling_service import run_escalation_sweep

        def job():
            with SessionLocal() as db:
                run_escalation_sweep(db)

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(job, "interval", minutes=s.scheduler_interval_minutes, id="escalation_sweep",
                          max_instances=1, coalesce=True)
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, version="1.0.0", lifespan=lifespan,
                  description="Reasoning-driven healthcare referral closure backend. "
                              "AI recommends; humans decide. Errors use {\"error\": {code, message, details}}.")
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code, headers=(
            {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None),
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, headers=getattr(exc, "headers", None),
                            content={"error": {"code": {404: "not_found", 405: "method_not_allowed"}.get(
                                exc.status_code, "http_error"), "message": str(exc.detail), "details": None}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"],
                    "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error",
                                                                "message": "Request validation failed",
                                                                "details": details}})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        logger.exception("Unhandled error")  # no request bodies / patient data logged
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error",
                                                                "message": "Internal server error", "details": None}})

    for r in (auth.router, facilities.router, referrals.router, followups.router, escalations.router,
              dashboard.router, audit.router):
        app.include_router(r)

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
