import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    env: str
    admin_password: Optional[str]
    session_secret: str
    session_max_age_seconds: int
    db_path: str
    base_url: str
    email_from: str
    smtp_host: Optional[str]
    smtp_port: int
    smtp_username: Optional[str]
    smtp_password: Optional[str]
    smtp_use_tls: bool
    internal_api_token: Optional[str]


def get_settings() -> Settings:
    env = os.getenv("NR3_ENV", "development").strip().lower() or "development"
    admin_password = os.getenv("NR3_ADMIN_PASSWORD")
    session_secret = os.getenv("NR3_SESSION_SECRET")

    if env == "production" and not session_secret:
        raise RuntimeError("NR3_SESSION_SECRET is required in production.")
    if not session_secret:
        session_secret = "dev-only-change-me"

    return Settings(
        env=env,
        admin_password=admin_password,
        session_secret=session_secret,
        session_max_age_seconds=12 * 60 * 60,
        db_path=os.getenv("NR3_DB_PATH", "data/nr3.db"),
        base_url=os.getenv("NR3_BASE_URL", "http://127.0.0.1:8010").rstrip("/"),
        email_from=os.getenv("NR3_EMAIL_FROM", "onboarding@unboks.org"),
        smtp_host=_clean_env("NR3_SMTP_HOST"),
        smtp_port=int(os.getenv("NR3_SMTP_PORT", "587")),
        smtp_username=_clean_env("NR3_SMTP_USERNAME"),
        smtp_password=_clean_env("NR3_SMTP_PASSWORD"),
        smtp_use_tls=os.getenv("NR3_SMTP_USE_TLS", "true").strip().lower()
        not in {"0", "false", "no", "off"},
        internal_api_token=_clean_env("NR3_INTERNAL_API_TOKEN"),
    )


def _clean_env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None
