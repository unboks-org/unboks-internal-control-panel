from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.emailer import build_tenant_welcome_email, send_email, smtp_is_configured
from app.icp_overrides import initialize_new_tenant_fail_closed
from app.port_registry import release_tenant_port, reserve_tenant_port
from app.provisioning import (
    AutoProvisionResult,
    auto_provision_enabled,
    auto_provision_tenant,
    clear_tenant_provision_claim,
    create_tenant_provision_claim,
    tenant_creation_lock,
    tenant_provision_claim,
    update_tenant_provision_claim_job,
)
from app.tenants import (
    RESERVED_SLUGS,
    TenantCreateError,
    derive_slug_from_name,
    forget_tenant_state,
    register_tenant,
    tenant_slug_exists_for_creation,
    validate_slug,
    write_private_client_json,
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
    creation_id: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _slug_candidates(business_name: str):
    base = derive_slug_from_name(business_name) or "client"
    base = validate_slug(base[:50])
    if base in RESERVED_SLUGS:
        raise TenantCreateError(f"Tenant slug {base!r} is reserved.")
    for index in range(0, 100):
        candidate = base if index == 0 else validate_slug(f"{base[:44]}-{index}")
        if candidate not in RESERVED_SLUGS:
            yield candidate


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
        "      - TENANT_RUNTIME_CONTROLS_REQUIRED=true\n"
        "      - TENANT_ACCOUNT_ALLOWLIST_REQUIRED=true\n"
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
        "    proxy_hide_header X-Unboks-Tenant;\n"
        f'    add_header X-Unboks-Tenant "{slug}" always;\n'
        '    add_header Access-Control-Expose-Headers "X-Unboks-Tenant" always;\n'
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
    config_path = os.path.join(tenant_dir, "config", "client.json")
    write_private_client_json(config_path, client_data)


def _provisioning_allows_welcome(result: AutoProvisionResult) -> bool:
    return result.status == "succeeded"


def create_public_signup_tenant(
    *,
    full_name: str,
    business_name: str,
    email: str,
    phone: str,
    settings: Settings,
    signup_request_id: str = "",
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
    if signup_request_id and not auto_provision_enabled():
        raise TenantCreateError(
            "Automatic tenant provisioning is disabled; the signup remains ready "
            "for review and no workspace was created."
        )

    assigned_slug = ""
    if signup_request_id:
        from app.public_signup_requests import get_signup_request

        signup_record = get_signup_request(signup_request_id, settings)
        raw_assigned_slug = str(signup_record.get("provisioned_slug") or "").strip()
        if raw_assigned_slug:
            assigned_slug = validate_slug(raw_assigned_slug)

    candidates = (assigned_slug,) if assigned_slug else _slug_candidates(clean_name)
    for slug in candidates:
        with tenant_creation_lock(slug):
            if tenant_slug_exists_for_creation(slug) or tenant_provision_claim(slug):
                if assigned_slug:
                    raise TenantCreateError(
                        "This signup's original workspace slug is still reserved. "
                        "Reconcile the failed host job before retrying; a second "
                        "workspace was not created."
                    )
                continue

            password = secrets.token_urlsafe(12)
            access_key = secrets.token_urlsafe(24)
            creation_id = secrets.token_urlsafe(18)
            created = _now()
            whatsapp_connect_token = secrets.token_urlsafe(32)
            whatsapp_connect_token_expires_at = (
                created + timedelta(days=30)
            ).isoformat()
            trial_ends = created + timedelta(days=14)
            if not create_tenant_provision_claim(slug, creation_id):
                continue
            try:
                host_port = reserve_tenant_port(slug)
            except Exception:
                clear_tenant_provision_claim(slug, creation_id)
                release_tenant_port(slug)
                raise
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
                "channel_account_allowlist": {
                    "mode": "strict",
                    "zernio_accounts": [],
                    "notes": (
                        "No provider account is authorized until Nr3 verifies "
                        "and selects it."
                    ),
                },
            }
            if clean_phone:
                client_data["whatsapp"] = clean_phone

            try:
                from app.delete_operations import bind_tenant_generation_for_creation

                bind_tenant_generation_for_creation(
                    slug=slug,
                    generation_id=creation_id,
                )
                initialize_new_tenant_fail_closed(slug)
                _write_registry_file(client_data)
                # Register a fail-closed placeholder before a potentially
                # asynchronous worker result so the slug remains owned.
                register_tenant(client_data)
                if signup_request_id:
                    from app.public_signup_requests import mark_provisioning_started

                    mark_provisioning_started(
                        signup_request_id,
                        slug=slug,
                        creation_id=creation_id,
                        initial_password=password,
                        settings=settings,
                    )
                provision_result = auto_provision_tenant(
                    slug=slug,
                    host_port=host_port,
                    client_data=client_data,
                    docker_compose_text=_docker_compose_text(slug, host_port),
                    managed_nginx_block_text=_managed_nginx_block_text(slug, host_port),
                    dashboard_url=dashboard_url,
                    creation_id=creation_id,
                    signup_request_id=signup_request_id,
                )
            except Exception as exc:
                if clear_tenant_provision_claim(slug, creation_id):
                    forget_tenant_state(slug)
                if signup_request_id:
                    from app.public_signup_requests import (
                        reconcile_signup_provisioning_result,
                    )

                    reconcile_signup_provisioning_result(
                        signup_request_id,
                        slug=slug,
                        creation_id=creation_id,
                        job_id="",
                        status="failed",
                        message=str(exc)[:300] or type(exc).__name__,
                        settings=settings,
                    )
                raise

            if provision_result.status not in {
                "succeeded",
                "failed",
                "queued",
                "disabled",
            }:
                provision_result = AutoProvisionResult(
                    status="queued",
                    message=(
                        "Provisioning returned an unknown state; tenant ownership "
                        "was retained for safe operator reconciliation."
                    ),
                    job_id=provision_result.job_id,
                    details=provision_result.details,
                    dashboard_url=provision_result.dashboard_url,
                    health_url=provision_result.health_url,
                )

            if provision_result.status == "failed":
                if signup_request_id:
                    from app.public_signup_requests import (
                        reconcile_signup_provisioning_result,
                    )

                    reconcile_signup_provisioning_result(
                        signup_request_id,
                        slug=slug,
                        creation_id=creation_id,
                        job_id=str(provision_result.job_id or ""),
                        status="failed",
                        message=provision_result.message,
                        settings=settings,
                    )
                if getattr(provision_result, "safe_to_release", False) is True:
                    if clear_tenant_provision_claim(slug, creation_id):
                        forget_tenant_state(slug)
            elif provision_result.status == "queued" or (
                provision_result.status == "disabled" and signup_request_id
            ):
                pending_job_id = str(
                    provision_result.job_id or f"manual-{creation_id}"
                )
                update_tenant_provision_claim_job(
                    slug, creation_id, pending_job_id
                )
                if signup_request_id:
                    from app.public_signup_requests import mark_provisioning_pending

                    mark_provisioning_pending(
                        signup_request_id,
                        slug=slug,
                        job_id=pending_job_id,
                        creation_id=creation_id,
                        settings=settings,
                    )
            elif provision_result.status in {"succeeded", "disabled"}:
                from app.delete_operations import activate_tenant_generation

                activate_tenant_generation(
                    slug=slug,
                    generation_id=creation_id,
                )
                clear_tenant_provision_claim(slug, creation_id)

            welcome_status = "not_sent"
            welcome_error = ""
            if signup_request_id:
                welcome_status = (
                    "provisioning_pending"
                    if provision_result.status in {"queued", "disabled"}
                    else "deferred_to_signup_reconciler"
                )
            elif _provisioning_allows_welcome(provision_result):
                if smtp_is_configured(settings):
                    draft = build_tenant_welcome_email(
                        tenant_name=clean_name,
                        dashboard_url=dashboard_url,
                        username=slug,
                        initial_token=password,
                        custom_message=(
                            "Your 14-day trial is active. When you sign in, the "
                            "dashboard will guide you through WhatsApp connection "
                            "and Agent style setup."
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
            else:
                welcome_error = (
                    "Workspace provisioning did not complete; welcome email was not sent."
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
                creation_id=creation_id,
            )

    raise TenantCreateError("Could not generate a unique tenant slug.")
