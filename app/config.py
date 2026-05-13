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
    )
