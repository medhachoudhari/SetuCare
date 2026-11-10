"""Application configuration (environment driven, no secrets in code)."""
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULT_KEY = "dev-only-insecure-secret-key-change-me-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "SetuCare API"
    environment: str = "development"
    database_url: str = "sqlite:///./setucare_dev.db"
    auto_create_tables: bool = True
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # --- auth ---
    secret_key: str = _INSECURE_DEFAULT_KEY
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    allow_public_registration: bool = True
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    bcrypt_rounds: int = 12
    login_max_attempts: int = 5
    login_window_seconds: int = 900

    # --- scheduler ---
    enable_scheduler: bool = False
    scheduler_interval_minutes: int = 15

    # --- deadline policy: hours from creation, keyed by human-assigned urgency ---
    deadline_hours_emergency: float = 6
    deadline_hours_urgent: float = 24
    deadline_hours_semi_urgent: float = 72
    deadline_hours_routine: float = 168
    # --- max interval between follow-up attempts (AI may only shorten it) ---
    followup_interval_hours_emergency: float = 1
    followup_interval_hours_urgent: float = 6
    followup_interval_hours_semi_urgent: float = 24
    followup_interval_hours_routine: float = 48
    max_followup_attempts: int = 4

    # --- AI integration ---
    ai_agent_provider: str = "mock"
    ai_confidence_threshold: float = 0.6

    @property
    def deadline_hours(self) -> dict[str, float]:
        return {
            "emergency": self.deadline_hours_emergency,
            "urgent": self.deadline_hours_urgent,
            "semi_urgent": self.deadline_hours_semi_urgent,
            "routine": self.deadline_hours_routine,
        }

    @property
    def followup_interval_hours(self) -> dict[str, float]:
        return {
            "emergency": self.followup_interval_hours_emergency,
            "urgent": self.followup_interval_hours_urgent,
            "semi_urgent": self.followup_interval_hours_semi_urgent,
            "routine": self.followup_interval_hours_routine,
        }

    @model_validator(mode="after")
    def _check_production_secret(self):
        if self.environment.lower() == "production" and (
            self.secret_key == _INSECURE_DEFAULT_KEY or len(self.secret_key) < 32
        ):
            raise ValueError("SECRET_KEY must be set to a random value (>=32 chars) in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
