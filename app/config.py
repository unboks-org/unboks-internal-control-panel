import os
from dataclasses import dataclass, field
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
    tenant_bridge_token_dir: str
    allow_legacy_shared_bridge_token: bool
    zernio_api_key: Optional[str] = field(repr=False)
    zernio_api_base_url: str
    unboks_public_url: str
    unboks_admin_api_url: str
    zernio_webhook_secret: Optional[str] = field(default=None, repr=False)
    late_api_key: Optional[str] = field(default=None, repr=False)
    public_signup_auto_provision_after_verify: bool = False
    public_signup_requests_path: str = "data/public_signup_requests.json"
    public_signup_rate_limit_per_ip_per_hour: int = 5
    public_signup_rate_limit_per_email_per_day: int = 3
    public_signup_verification_ttl_hours: int = 48


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
        tenant_bridge_token_dir=os.getenv(
            "NR3_TENANT_BRIDGE_TOKEN_DIR",
            "/app/tenant_root/_shared/nr3_bridge_tokens",
        ).strip()
        or "/app/tenant_root/_shared/nr3_bridge_tokens",
        allow_legacy_shared_bridge_token=os.getenv(
            "NR3_ALLOW_LEGACY_SHARED_BRIDGE_TOKEN",
            "false",
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        zernio_api_key=_clean_env("ZERNIO_API_KEY"),
        zernio_webhook_secret=_clean_env("ZERNIO_WEBHOOK_SECRET"),
        late_api_key=_clean_env("LATE_API_KEY") or _clean_env("ZERNIO_API_KEY"),
        public_signup_auto_provision_after_verify=os.getenv(
            "NR3_PUBLIC_SIGNUP_AUTO_PROVISION_AFTER_VERIFY",
            "false",
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        public_signup_requests_path=os.getenv(
            "NR3_PUBLIC_SIGNUP_REQUESTS_PATH",
            "data/public_signup_requests.json",
        ).strip()
        or "data/public_signup_requests.json",
        public_signup_rate_limit_per_ip_per_hour=int(os.getenv(
            "NR3_PUBLIC_SIGNUP_RATE_LIMIT_PER_IP_PER_HOUR",
            "5",
        )),
        public_signup_rate_limit_per_email_per_day=int(os.getenv(
            "NR3_PUBLIC_SIGNUP_RATE_LIMIT_PER_EMAIL_PER_DAY",
            "3",
        )),
        public_signup_verification_ttl_hours=max(1, int(os.getenv(
            "NR3_PUBLIC_SIGNUP_VERIFICATION_TTL_HOURS",
            "48",
        ))),
        zernio_api_base_url=os.getenv(
            "ZERNIO_API_BASE_URL",
            "https://zernio.com/api/v1",
        ).strip().rstrip("/"),
        unboks_public_url=os.getenv(
            "UNBOKS_PUBLIC_URL",
            "https://unboks.org",
        ).strip().rstrip("/"),
        unboks_admin_api_url=os.getenv(
            "UNBOKS_ADMIN_API_URL",
            "https://icp.unboks.org/internal/api",
        ).strip().rstrip("/"),
    )


def _clean_env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None
