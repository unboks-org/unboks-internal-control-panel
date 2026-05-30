from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.emailer import build_tenant_welcome_email, send_email, smtp_is_configured
from app.port_registry import reserve_tenant_port
from app.provisioning import AutoProvisionResult, auto_provision_tenant
from app.tenants import (
    TenantCreateError,
    derive_slug_from_name,
    get_tenant,
    register_tenant,
    validate_slug,
)


@dataclass(frozen=True)
class SignupResult:
    slug: str
    name: str
    email: str
    dashboard_url: str
    password: str
    access_key: str
    trial_ends_at: str
    welcome_status: str
    welcome_error: str
    provision_result: AutoProvisionResult


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _tenant_root_exists(slug: str) -> bool:
    root = (os.environ.get("NR3_TENANTS_CLIENT_DIR") or "/root/clients").strip()
    return bool(root and os.path.exists(os.path.join(root, slug)))


def _unique_slug(business_name: str) -> str:
    base = derive_slug_from_name(business_name) or "client"
    base = validate_slug(base[:50])
    for index in range(0, 100):
        candidate = base if index == 0 else validate_slug(f"{base[:44]}-{index}")
        if get_tenant(candidate) is None and not _tenant_root_exists(candidate):
            return candidate
    raise TenantCreateError("Could not generate a unique tenant slug.")


def _docker_compose_text(slug: str, host_port: int) -> str:
    return (
        f"# docker-compose.yml for tenant {slug}\n"
        "services:\n"
        "  agent:\n"
        "    image: wtyj-agent\n"
        f"    container_name: wtyj-{slug}\n"
        "    restart: unless-stopped\n"
        "    ports:\n"
        f'      - "127.0.0.1:{host_port}:8001"\n'
        "    env_file:\n"
        "      - ./config/platform.env\n"
        "    environment:\n"
        "      - GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/app/config/calendar-key.json\n"
        "    volumes:\n"
        "      - ./config:/app/config:rw\n"
        "      - ./data:/app/data\n"
        "      - ./logs:/app/logs\n"
        "    networks:\n"
        "      - default\n"
        "      - unboks-control\n"
        "networks:\n"
        "  unboks-control:\n"
        "    external: true\n"
    )


def _managed_nginx_block_text(slug: str, host_port: int) -> str:
    snippet = (
        f"# nginx route for tenant {slug}\n"
        f"location ^~ /api/{slug}/ {{\n"
        f"    proxy_set_header X-Tenant-Slug {slug};\n"
        "\n"
        "    if ($request_method = OPTIONS) {\n"
        '        add_header Access-Control-Allow-Origin "https://dashboard.unboks.org" always;\n'
        '        add_header Access-Control-Allow-Methods "GET, POST, PUT, PATCH, DELETE, OPTIONS" always;\n'
        '        add_header Access-Control-Allow-Headers "Authorization, Content-Type, Accept, Origin, X-Tenant-Slug, Cache-Control, Pragma" always;\n'
        '        add_header Access-Control-Allow-Credentials "true" always;\n'
        "        add_header Access-Control-Max-Age 86400 always;\n"
        "        return 204;\n"
        "    }\n"
        "\n"
        f"    proxy_pass http://127.0.0.1:{host_port}/;\n"
        "    proxy_set_header Host $host;\n"
        "    proxy_set_header X-Real-IP $remote_addr;\n"
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "    proxy_set_header X-Forwarded-Proto $scheme;\n"
        "}\n"
    )
    return (
        f"    # BEGIN UNBOKS TENANT {slug}\n"
        + "\n".join(("    " + line if line else "") for line in snippet.rstrip().splitlines())
        + f"\n    # END UNBOKS TENANT {slug}\n"
    )


def _write_registry_file(client_data: dict[str, Any]) -> None:
    if (os.environ.get("NR3_AUTO_PROVISION") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return
    root = (os.environ.get("NR3_TENANTS_CLIENT_DIR") or "/root/clients").strip()
    if not root:
        return
    slug = str(client_data["slug"])
    tenant_dir = os.path.join(root, slug)
    if os.path.exists(tenant_dir):
        return
    os.makedirs(os.path.join(tenant_dir, "config"), exist_ok=True)
    os.makedirs(os.path.join(tenant_dir, "data"), exist_ok=True)
    with open(os.path.join(tenant_dir, "config", "client.json"), "w", encoding="utf-8") as f:
        json.dump(client_data, f, indent=2, ensure_ascii=False)


def create_public_signup_tenant(
    *,
    full_name: str,
    business_name: str,
    email: str,
    phone: str,
    settings: Settings,
) -> SignupResult:
    clean_name = business_name.strip()
    clean_email = email.strip().lower()
    clean_full_name = full_name.strip()
    clean_phone = phone.strip()
    if not clean_full_name:
        raise TenantCreateError("Full name is required.")
    if not clean_name:
        raise TenantCreateError("Business name is required.")
    if "@" not in clean_email or "." not in clean_email:
        raise TenantCreateError("A valid email is required.")

    slug = _unique_slug(clean_name)
    password = secrets.token_urlsafe(12)
    access_key = secrets.token_urlsafe(24)
    created = _now()
    whatsapp_connect_token = secrets.token_urlsafe(32)
    whatsapp_connect_token_expires_at = (created + timedelta(days=30)).isoformat()
    trial_ends = created + timedelta(days=14)
    host_port = reserve_tenant_port(slug)
    dashboard_url = f"https://dashboard.unboks.org/login?workspace={slug}"

    client_data: dict[str, Any] = {
        "slug": slug,
        "name": clean_name,
        "password": password,
        "access_key": access_key,
        "status": "active",
        "plan": "active",
        "billing_status": "trialing",
        "trial_status": "active",
        "trial_started_at": created.isoformat(),
        "trial_ends_at": trial_ends.isoformat(),
        "created_at": created.isoformat(),
        "host_port": host_port,
        "contact_person": clean_full_name,
        "email": clean_email,
        "whatsapp_connect_token": whatsapp_connect_token,
        "whatsapp_connect_token_expires_at": whatsapp_connect_token_expires_at,
        "business": {
            "slug": slug,
            "name": clean_name,
            "email": clean_email,
            "phone": clean_phone,
        },
    }
    if clean_phone:
        client_data["whatsapp"] = clean_phone

    _write_registry_file(client_data)
    register_tenant(client_data)

    welcome_status = "unchecked"
    welcome_error = ""
    if smtp_is_configured(settings):
        draft = build_tenant_welcome_email(
            tenant_name=clean_name,
            dashboard_url=dashboard_url,
            username=slug,
            initial_token=password,
            custom_message=(
                "Your 14-day trial is active. When you sign in, the dashboard "
                "will guide you through WhatsApp connection and Agent style setup."
            ),
        )
        try:
            send_email(clean_email, draft.subject, draft.body, settings)
            welcome_status = "sent"
        except Exception as exc:
            welcome_status = "failed"
            welcome_error = str(exc)
    else:
        welcome_status = "no_smtp"

    provision_result = auto_provision_tenant(
        slug=slug,
        host_port=host_port,
        client_data=client_data,
        docker_compose_text=_docker_compose_text(slug, host_port),
        managed_nginx_block_text=_managed_nginx_block_text(slug, host_port),
        dashboard_url=dashboard_url,
    )

    return SignupResult(
        slug=slug,
        name=clean_name,
        email=clean_email,
        dashboard_url=dashboard_url,
        password=password,
        access_key=access_key,
        trial_ends_at=trial_ends.isoformat(),
        welcome_status=welcome_status,
        welcome_error=welcome_error,
        provision_result=provision_result,
    )
