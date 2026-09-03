from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.file_lock import exclusive_file_lock
from app.tenants import TenantCreateError, derive_slug_from_name


_CREDENTIAL_DELIVERY_LEASE = timedelta(minutes=5)
_CREDENTIAL_DELIVERY_RETRYABLE_STATUSES = {
    "pending",
    "failed",
    "no_smtp",
    "not_sent",
}


@dataclass(frozen=True)
class SignupRequest:
    id: str
    full_name: str
    business_name: str
    email: str
    phone: str
    slug_hint: str
    status: str
    created_at: str
    updated_at: str
    token_expires_at: str
    token: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _credential_secret_digest(
    *,
    slug: str,
    creation_id: str,
    secret: str,
) -> str:
    if not slug or not creation_id or not secret:
        return ""
    material = f"v1\0{creation_id}\0{slug}\0{secret}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def client_ip_from_headers(headers: dict[str, str], fallback: str) -> str:
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or fallback
    return headers.get("x-real-ip", "").strip() or fallback


def create_signup_request(
    *,
    full_name: str,
    business_name: str,
    email: str,
    phone: str,
    ip_address: str,
    user_agent: str,
    settings: Settings,
) -> SignupRequest:
    clean_full_name = full_name.strip()
    clean_business = business_name.strip()
    clean_email = normalize_email(email)
    clean_phone = phone.strip()
    if not clean_full_name:
        raise TenantCreateError("Full name is required.")
    if not clean_business:
        raise TenantCreateError("Business name is required.")
    if "@" not in clean_email or "." not in clean_email:
        raise TenantCreateError("A valid email is required.")

    token = secrets.token_urlsafe(32)
    now_dt = utc_now()
    now = now_dt.isoformat()
    token_expires_at = (
        now_dt + timedelta(hours=settings.public_signup_verification_ttl_hours)
    ).isoformat()
    request_id = secrets.token_urlsafe(16)
    slug_hint = derive_slug_from_name(clean_business) or "client"
    record = {
        "id": request_id,
        "full_name": clean_full_name,
        "business_name": clean_business,
        "email": clean_email,
        "phone": clean_phone,
        "slug_hint": slug_hint,
        "status": "verification_pending",
        "created_at": now,
        "updated_at": now,
        "token_expires_at": token_expires_at,
        "verified_at": None,
        "provisioned_at": None,
        "token_hash": _hash_token(token),
        "ip_address": ip_address,
        "user_agent": user_agent[:300],
    }
    with _locked_store(settings) as data:
        _enforce_rate_limits(data, clean_email, ip_address, settings)
        requests = data.setdefault("requests", {})
        if not isinstance(requests, dict):
            requests = {}
            data["requests"] = requests
        requests[request_id] = record
    return SignupRequest(
        id=request_id,
        full_name=clean_full_name,
        business_name=clean_business,
        email=clean_email,
        phone=clean_phone,
        slug_hint=slug_hint,
        status="verification_pending",
        created_at=now,
        updated_at=now,
        token_expires_at=token_expires_at,
        token=token,
    )


def mark_verified(token: str, settings: Settings) -> dict[str, Any]:
    token_hash = _hash_token(token.strip())
    if not token_hash:
        raise TenantCreateError("Invalid verification link.")
    expired = False
    matched: dict[str, Any] | None = None
    with _locked_store(settings) as data:
        requests = data.get("requests")
        if not isinstance(requests, dict):
            raise TenantCreateError("Invalid verification link.")
        for request_id, record in requests.items():
            if not isinstance(record, dict) or record.get("token_hash") != token_hash:
                continue
            now = utc_now().isoformat()
            status = str(record.get("status") or "")
            if status in {
                "provisioned",
                "provisioning_dispatching",
                "provisioning_pending",
                "approved",
                "onboarding_link_generated",
                "onboarding_link_sent",
                "onboarding_link_generating",
                "onboarding_email_sending",
                "info_request_sending",
                "failed",
                "rejected",
                "archived",
            }:
                return dict(record)
            if status == "verification_expired" or _verification_expired(record, settings):
                record["status"] = "verification_expired"
                record["updated_at"] = now
                requests[request_id] = record
                expired = True
                break
            record["status"] = "verified_pending_review"
            record["verified_at"] = record.get("verified_at") or now
            record["updated_at"] = now
            requests[request_id] = record
            matched = dict(record)
            break
    if expired:
        raise TenantCreateError("Invalid or expired verification link.")
    if matched is not None:
        return matched
    raise TenantCreateError("Invalid or expired verification link.")


def mark_provisioned(request_id: str, slug: str, settings: Settings) -> None:
    with _locked_store(settings) as data:
        requests = data.get("requests")
        if not isinstance(requests, dict):
            return
        record = requests.get(request_id)
        if not isinstance(record, dict):
            return
        now = utc_now().isoformat()
        record["status"] = "provisioned"
        record["provisioned_slug"] = slug
        record["provisioned_at"] = record.get("provisioned_at") or now
        record.setdefault("credential_delivery_status", "pending")
        record["updated_at"] = now


def mark_provisioning_started(
    request_id: str,
    *,
    slug: str,
    creation_id: str,
    initial_password: str,
    settings: Settings,
) -> dict[str, Any]:
    """Claim one signup before its host job is dispatched."""
    with _locked_store(settings) as data:
        requests = data.get("requests")
        record = requests.get(request_id) if isinstance(requests, dict) else None
        if not isinstance(record, dict):
            raise TenantCreateError("Signup request not found.")
        existing_creation = str(record.get("provisioning_creation_id") or "")
        status = str(record.get("status") or "")
        if status == "provisioned":
            raise TenantCreateError("This signup already has a provisioned workspace.")
        if status in {"provisioning_dispatching", "provisioning_pending"}:
            if existing_creation != creation_id:
                raise TenantCreateError(
                    "A different workspace creation is already active for this signup."
                )
            return _safe_record(record)
        if status not in {
            "verified_pending_review",
            "approved",
            "onboarding_link_generated",
            "onboarding_link_sent",
            "failed",
        }:
            raise TenantCreateError(
                "This signup is not eligible for workspace creation."
            )
        now = utc_now().isoformat()
        record.update({
            "status": "provisioning_dispatching",
            "provisioned_slug": slug,
            "provisioning_creation_id": creation_id,
            "provisioning_job_id": "",
            "workspace_error": "",
            "credential_delivery_status": "pending",
            "credential_delivery_attempt_id": "",
            "credential_delivery_attempt_count": 0,
            "credential_delivery_lease_expires_at": None,
            "credential_delivery_last_attempt_at": None,
            "credential_delivery_sent_at": None,
            "credential_delivery_error": "",
            "credential_delivery_secret_digest": _credential_secret_digest(
                slug=slug,
                creation_id=creation_id,
                secret=initial_password,
            ),
            "updated_at": now,
        })
        return _safe_record(record)


def mark_provisioning_pending(
    request_id: str,
    *,
    slug: str,
    job_id: str,
    creation_id: str,
    settings: Settings,
) -> dict[str, Any]:
    """Bind one signup to one asynchronous workspace creation idempotently."""
    with _locked_store(settings) as data:
        requests = data.get("requests")
        record = requests.get(request_id) if isinstance(requests, dict) else None
        if not isinstance(record, dict):
            raise TenantCreateError("Signup request not found.")
        status = str(record.get("status") or "")
        if status in {"provisioned", "failed"}:
            return _safe_record(record)
        existing_job = str(record.get("provisioning_job_id") or "")
        existing_creation = str(record.get("provisioning_creation_id") or "")
        if existing_creation and existing_creation != creation_id:
            raise TenantCreateError(
                "A different workspace creation is already active for this signup."
            )
        if status == "provisioning_pending" and existing_job:
            if existing_job != job_id:
                raise TenantCreateError(
                    "A different workspace creation is already pending for this signup."
                )
            return _safe_record(record)
        if status != "provisioning_dispatching":
            raise TenantCreateError(
                "Signup state changed before workspace dispatch could finish."
            )
        now = utc_now().isoformat()
        record.update({
            "status": "provisioning_pending",
            "provisioned_slug": slug,
            "provisioning_job_id": job_id,
            "provisioning_creation_id": creation_id,
            "workspace_error": "",
            "credential_delivery_status": "pending",
            "updated_at": now,
        })
        return _safe_record(record)


def reconcile_signup_provisioning_result(
    request_id: str,
    *,
    slug: str,
    creation_id: str,
    job_id: str,
    status: str,
    message: str,
    settings: Settings,
) -> bool:
    """Apply one owned worker result and resume its credential outbox.

    Provisioning completion and email delivery are deliberately separate
    durable states. A replay of the same successful worker result may reclaim
    a failed or expired delivery lease, while a live lease prevents concurrent
    reconcilers from sending the credentials twice.
    """
    if status not in {"succeeded", "failed"}:
        return False

    should_resume_delivery = False
    with _locked_store(settings) as data:
        requests = data.get("requests")
        record = requests.get(request_id) if isinstance(requests, dict) else None
        if not isinstance(record, dict):
            return False
        if (
            str(record.get("provisioned_slug") or "") != slug
            or str(record.get("provisioning_creation_id") or "") != creation_id
        ):
            return False
        stored_job_id = str(record.get("provisioning_job_id") or "")
        if stored_job_id and stored_job_id != job_id:
            return False
        current_status = str(record.get("status") or "")
        if current_status == "provisioned" and status == "succeeded":
            if not stored_job_id:
                record["provisioning_job_id"] = job_id
            should_resume_delivery = True
        elif current_status == "failed" and status == "failed":
            return True
        elif current_status not in {
            "provisioning_dispatching",
            "provisioning_pending",
        }:
            return False
        else:
            now = utc_now().isoformat()
            record["provisioning_job_id"] = job_id
            record["updated_at"] = now
            if status == "failed":
                record["status"] = "failed"
                record["workspace_error"] = (
                    message or "Workspace provisioning failed."
                )
                record["credential_delivery_status"] = "not_sent"
                record["credential_delivery_attempt_id"] = ""
                record["credential_delivery_lease_expires_at"] = None
                return True

            record["status"] = "provisioned"
            record["provisioned_at"] = record.get("provisioned_at") or now
            record["workspace_error"] = ""
            record.setdefault("credential_delivery_status", "pending")
            should_resume_delivery = True

    if not should_resume_delivery:
        return True
    return _resume_signup_credential_delivery(
        request_id,
        slug=slug,
        creation_id=creation_id,
        job_id=job_id,
        settings=settings,
    )


def retry_signup_credential_delivery(
    request_id: str,
    settings: Settings,
) -> dict[str, Any]:
    """Retry credentials for an already-provisioned, owned signup workspace."""
    record = get_signup_request(request_id, settings)
    if str(record.get("status") or "") != "provisioned":
        raise TenantCreateError(
            "Workspace credentials can be retried only after provisioning succeeds."
        )
    slug = str(record.get("provisioned_slug") or "").strip()
    creation_id = str(record.get("provisioning_creation_id") or "").strip()
    job_id = str(record.get("provisioning_job_id") or "").strip()
    if not slug or not creation_id:
        raise TenantCreateError(
            "Workspace credential ownership metadata is incomplete; retry was blocked."
        )
    if not _resume_signup_credential_delivery(
        request_id,
        slug=slug,
        creation_id=creation_id,
        job_id=job_id,
        settings=settings,
    ):
        raise TenantCreateError(
            "Signup state changed; workspace credentials were not sent."
        )
    return get_signup_request(request_id, settings)


def _resume_signup_credential_delivery(
    request_id: str,
    *,
    slug: str,
    creation_id: str,
    job_id: str,
    settings: Settings,
) -> bool:
    """Claim, perform, and finalize one lease-owned credential email attempt."""
    now_dt = utc_now()
    now = now_dt.isoformat()
    attempt_id = ""
    expected_secret_digest = ""
    with _locked_store(settings) as data:
        requests = data.get("requests")
        record = requests.get(request_id) if isinstance(requests, dict) else None
        if not isinstance(record, dict):
            return False
        if (
            str(record.get("status") or "") != "provisioned"
            or str(record.get("provisioned_slug") or "") != slug
            or str(record.get("provisioning_creation_id") or "") != creation_id
            or str(record.get("provisioning_job_id") or "") != job_id
        ):
            return False

        delivery_status = str(
            record.get("credential_delivery_status") or "pending"
        )
        if delivery_status == "sent":
            return True
        lease_expires_at = _parse_time(
            str(record.get("credential_delivery_lease_expires_at") or "")
        )
        if (
            delivery_status == "sending"
            and lease_expires_at is not None
            and lease_expires_at > now_dt
        ):
            # Another process still owns the attempt. Returning success here lets
            # result reconciliation complete without creating a duplicate send.
            return True
        if (
            delivery_status != "sending"
            and delivery_status not in _CREDENTIAL_DELIVERY_RETRYABLE_STATUSES
        ):
            return False

        try:
            attempt_count = int(record.get("credential_delivery_attempt_count") or 0)
        except (TypeError, ValueError):
            attempt_count = 0
        expected_secret_digest = str(
            record.get("credential_delivery_secret_digest") or ""
        )
        attempt_id = secrets.token_urlsafe(18)
        record.update({
            "credential_delivery_status": "sending",
            "credential_delivery_attempt_id": attempt_id,
            "credential_delivery_attempt_count": max(0, attempt_count) + 1,
            "credential_delivery_lease_expires_at": (
                now_dt + _CREDENTIAL_DELIVERY_LEASE
            ).isoformat(),
            "credential_delivery_last_attempt_at": now,
            "credential_delivery_updated_at": now,
            "credential_delivery_error": "",
            "updated_at": now,
        })

    delivery_status = "failed"
    delivery_error = "Workspace credentials are unavailable."
    try:
        from app.emailer import (
            build_tenant_welcome_email,
            send_email,
            smtp_is_configured,
        )
        from app.tenants import get_tenant_client_data

        record = get_signup_request(request_id, settings)
        tenant_data = get_tenant_client_data(slug)
        recipient = normalize_email(str(record.get("email") or ""))
        tenant_slug = tenant_data.get("slug")
        raw_password = tenant_data.get("password")
        password = raw_password if isinstance(raw_password, str) else ""
        tenant_name = str(
            tenant_data.get("name") or record.get("business_name") or slug
        )
        dashboard_url = f"https://dashboard.unboks.org/login?workspace={slug}"
        actual_secret_digest = _credential_secret_digest(
            slug=slug,
            creation_id=creation_id,
            secret=password,
        )
        if tenant_slug != slug:
            delivery_error = (
                "Workspace identity does not match this signup; credentials were not sent."
            )
        elif not expected_secret_digest or not secrets.compare_digest(
            expected_secret_digest,
            actual_secret_digest,
        ):
            delivery_error = (
                "Workspace credential generation does not match this signup; "
                "credentials were not sent."
            )
        elif not recipient or not password:
            delivery_error = "Workspace credentials or recipient are unavailable."
        elif not smtp_is_configured(settings):
            delivery_status = "no_smtp"
            delivery_error = "SMTP is not configured; credentials were not sent."
        else:
            draft = build_tenant_welcome_email(
                tenant_name=tenant_name,
                dashboard_url=dashboard_url,
                username=slug,
                initial_token=password,
                custom_message=(
                    "Your 14-day trial is active. When you sign in, the dashboard "
                    "will guide you through WhatsApp connection and Agent style setup."
                ),
            )
            send_email(recipient, draft.subject, draft.body, settings)
            delivery_status = "sent"
            delivery_error = ""
    except Exception as exc:
        delivery_error = str(exc)[:300] or type(exc).__name__

    finished_at = utc_now().isoformat()
    with _locked_store(settings) as data:
        requests = data.get("requests")
        record = requests.get(request_id) if isinstance(requests, dict) else None
        if not isinstance(record, dict):
            return False
        if (
            str(record.get("status") or "") != "provisioned"
            or str(record.get("provisioned_slug") or "") != slug
            or str(record.get("provisioning_creation_id") or "") != creation_id
            or str(record.get("provisioning_job_id") or "") != job_id
            or str(record.get("credential_delivery_secret_digest") or "")
            != expected_secret_digest
            or str(record.get("credential_delivery_status") or "") != "sending"
            or str(record.get("credential_delivery_attempt_id") or "") != attempt_id
        ):
            return False
        record["credential_delivery_status"] = delivery_status
        record["credential_delivery_attempt_id"] = ""
        record["credential_delivery_lease_expires_at"] = None
        record["credential_delivery_error"] = delivery_error
        record["credential_delivery_updated_at"] = finished_at
        if delivery_status == "sent":
            record["credential_delivery_sent_at"] = finished_at
        record["updated_at"] = finished_at
    return True


def mark_signup_creation_error(
    request_id: str,
    *,
    message: str,
    settings: Settings,
) -> dict[str, Any]:
    """Record an error without clobbering an owned in-flight or finished job."""
    with _locked_store(settings) as data:
        requests = data.get("requests")
        record = requests.get(request_id) if isinstance(requests, dict) else None
        if not isinstance(record, dict):
            raise TenantCreateError("Signup request not found.")
        if record.get("status") in {
            "provisioning_dispatching",
            "provisioning_pending",
            "provisioned",
            "rejected",
            "archived",
            "verification_expired",
            "onboarding_link_generating",
            "onboarding_email_sending",
            "info_request_sending",
        }:
            return _safe_record(record)
        record["status"] = "failed"
        record["workspace_error"] = message
        record["updated_at"] = utc_now().isoformat()
        return _safe_record(record)


def get_signup_request(request_id: str, settings: Settings) -> dict[str, Any]:
    data = _read_store(settings)
    requests = data.get("requests")
    if not isinstance(requests, dict):
        raise TenantCreateError("Signup request not found.")
    record = requests.get(request_id)
    if not isinstance(record, dict):
        raise TenantCreateError("Signup request not found.")
    return _safe_record(record)


def update_signup_request(
    request_id: str,
    settings: Settings,
    *,
    allowed_current_statuses: set[str] | frozenset[str] | None = None,
    **updates: Any,
) -> dict[str, Any]:
    with _locked_store(settings) as data:
        requests = data.get("requests")
        if not isinstance(requests, dict):
            raise TenantCreateError("Signup request not found.")
        record = requests.get(request_id)
        if not isinstance(record, dict):
            raise TenantCreateError("Signup request not found.")
        if (
            allowed_current_statuses is not None
            and str(record.get("status") or "") not in allowed_current_statuses
        ):
            raise TenantCreateError(
                "Signup state changed; the requested action was not applied."
            )
        now = utc_now().isoformat()
        record.update(updates)
        record["updated_at"] = now
        requests[request_id] = record
        return _safe_record(record)


def list_signup_requests(
    settings: Settings,
    *,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Return public signup requests without exposing verification token hashes."""
    store_path = Path(settings.public_signup_requests_path)
    lock_path = store_path.with_name(f"{store_path.name}.lock")
    with exclusive_file_lock(lock_path):
        data = _read_store(settings)
        requests = data.get("requests")
        if not isinstance(requests, dict):
            return []
        if _normalize_historical_email_tracking(data, settings):
            _write_store(data, settings)
        duplicate_meta = _duplicate_metadata(requests)
        safe_records: list[dict[str, Any]] = []
        for request_id, record in requests.items():
            if not isinstance(record, dict):
                continue
            if not include_archived and _hidden_from_active_queue(
                record, duplicate_meta.get(request_id, {})
            ):
                continue
            safe = _safe_record(record)
            safe.update(duplicate_meta.get(request_id, {}))
            safe_records.append(safe)
    return sorted(
        safe_records,
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )


def _safe_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key
        not in {
            "token_hash",
            "credential_delivery_attempt_id",
            "credential_delivery_secret_digest",
        }
    }


def is_archived_signup(record: dict[str, Any]) -> bool:
    return bool(record.get("archived_at")) or record.get("status") in {
        "archived",
        "rejected",
    }


def _hidden_from_active_queue(
    record: dict[str, Any],
    duplicate_info: dict[str, Any],
) -> bool:
    if is_archived_signup(record):
        return True
    if (
        record.get("status") == "provisioned"
        and record.get("credential_delivery_status") != "sent"
    ):
        # Delivery failures and stuck leases remain visible even if a newer
        # request would otherwise cause this signup to be folded as a duplicate.
        return False
    if duplicate_info.get("duplicate_hidden_by_default"):
        return True
    if record.get("status") == "provisioned":
        return True
    return record.get("status") == "verification_expired"


def _normalize_historical_email_tracking(data: dict[str, Any], settings: Settings) -> bool:
    """Mark legacy records as untracked without triggering any email delivery.

    The public signup send flow is event-based. Older records may lack the
    status fields added later; missing metadata must never be interpreted as a
    reason to send or resend messages.
    """
    requests = data.get("requests")
    if not isinstance(requests, dict):
        return False
    changed = False
    now = utc_now().isoformat()
    for request_id, record in requests.items():
        if not isinstance(record, dict):
            continue
        if not record.get("admin_alert_status"):
            record["admin_alert_status"] = "historical_untracked"
            record["admin_alert_sent_at"] = None
            record["admin_alert_error"] = (
                "Historical record created before admin alert tracking. No resend was triggered."
            )
            record["admin_alert_recipient"] = ""
            changed = True
        elif record.get("admin_alert_status") == "sent" and "admin_alert_recipient" not in record:
            record["admin_alert_recipient"] = settings.admin_alert_email or ""
            changed = True
        if not record.get("confirmation_email_status"):
            record["confirmation_email_status"] = "historical_untracked"
            record["confirmation_email_sent_at"] = None
            record["confirmation_email_recipient"] = record.get("email") or ""
            record["confirmation_email_error"] = (
                "Historical record created before confirmation email tracking. No resend was triggered."
            )
            changed = True
        requests[request_id] = record
    if changed:
        data["email_tracking_migrated_at"] = data.get("email_tracking_migrated_at") or now
    return changed


def _duplicate_metadata(requests: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for request_id, record in requests.items():
        if not isinstance(record, dict):
            continue
        email = normalize_email(str(record.get("email") or ""))
        slug = str(record.get("slug_hint") or derive_slug_from_name(str(record.get("business_name") or ""))).strip().lower()
        if not email or not slug:
            continue
        groups.setdefault((email, slug), []).append({"id": request_id, "record": record})

    metadata: dict[str, dict[str, Any]] = {}
    for items in groups.values():
        if len(items) < 2:
            continue
        sorted_items = sorted(
            items,
            key=lambda item: str(item["record"].get("created_at") or ""),
            reverse=True,
        )
        canonical_id = str(sorted_items[0]["id"])
        for index, item in enumerate(sorted_items):
            request_id = str(item["id"])
            metadata[request_id] = {
                "possible_duplicate": True,
                "duplicate_count": len(items),
                "duplicate_of": "" if index == 0 else canonical_id,
                "duplicate_hidden_by_default": index != 0,
            }
    return metadata


def _enforce_rate_limits(
    data: dict[str, Any],
    email: str,
    ip_address: str,
    settings: Settings,
) -> None:
    requests = data.get("requests")
    if not isinstance(requests, dict):
        return
    now = utc_now()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)
    ip_count = 0
    email_count = 0
    for record in requests.values():
        if not isinstance(record, dict):
            continue
        created = _parse_time(str(record.get("created_at") or ""))
        if created is None:
            continue
        if record.get("ip_address") == ip_address and created >= hour_ago:
            ip_count += 1
        if record.get("email") == email and created >= day_ago:
            email_count += 1
    if ip_count >= settings.public_signup_rate_limit_per_ip_per_hour:
        raise TenantCreateError("Too many signup attempts. Please try again later.")
    if email_count >= settings.public_signup_rate_limit_per_email_per_day:
        raise TenantCreateError("This email has too many signup attempts. Please try again later.")


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _verification_expired(record: dict[str, Any], settings: Settings) -> bool:
    expires_at = _parse_time(str(record.get("token_expires_at") or ""))
    if expires_at is None:
        created = _parse_time(str(record.get("created_at") or ""))
        if created is None:
            return True
        expires_at = created + timedelta(
            hours=settings.public_signup_verification_ttl_hours
        )
    return expires_at < utc_now()


def _hash_token(token: str) -> str:
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read_store(settings: Settings) -> dict[str, Any]:
    path = settings.public_signup_requests_path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"requests": {}}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise TenantCreateError("Signup request store is unreadable.") from exc
    if not isinstance(data, dict):
        raise TenantCreateError("Signup request store is malformed.")
    return data


def _write_store(data: dict[str, Any], settings: Settings) -> None:
    path = settings.public_signup_requests_path
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".public_signup.", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _locked_store(settings: Settings) -> Iterator[dict[str, Any]]:
    store_path = Path(settings.public_signup_requests_path)
    lock_path = store_path.with_name(f"{store_path.name}.lock")
    with exclusive_file_lock(lock_path):
        data = _read_store(settings)
        yield data
        _write_store(data, settings)
