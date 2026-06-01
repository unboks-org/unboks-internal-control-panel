from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.tenants import TenantCreateError, derive_slug_from_name


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

    data = _read_store(settings)
    _enforce_rate_limits(data, clean_email, ip_address, settings)

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
    requests = data.setdefault("requests", {})
    if not isinstance(requests, dict):
        requests = {}
        data["requests"] = requests
    requests[request_id] = record
    _write_store(data, settings)
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
    data = _read_store(settings)
    requests = data.get("requests")
    if not isinstance(requests, dict):
        raise TenantCreateError("Invalid verification link.")
    for request_id, record in requests.items():
        if isinstance(record, dict) and record.get("token_hash") == token_hash:
            now = utc_now().isoformat()
            if record.get("status") == "provisioned":
                return dict(record)
            if _verification_expired(record, settings):
                record["status"] = "verification_expired"
                record["updated_at"] = now
                requests[request_id] = record
                _write_store(data, settings)
                raise TenantCreateError("Invalid or expired verification link.")
            record["status"] = "verified_pending_review"
            record["verified_at"] = record.get("verified_at") or now
            record["updated_at"] = now
            requests[request_id] = record
            _write_store(data, settings)
            return dict(record)
    raise TenantCreateError("Invalid or expired verification link.")


def mark_provisioned(request_id: str, slug: str, settings: Settings) -> None:
    data = _read_store(settings)
    requests = data.get("requests")
    if not isinstance(requests, dict):
        return
    record = requests.get(request_id)
    if not isinstance(record, dict):
        return
    now = utc_now().isoformat()
    record["status"] = "provisioned"
    record["provisioned_slug"] = slug
    record["provisioned_at"] = now
    record["updated_at"] = now
    _write_store(data, settings)


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
    **updates: Any,
) -> dict[str, Any]:
    data = _read_store(settings)
    requests = data.get("requests")
    if not isinstance(requests, dict):
        raise TenantCreateError("Signup request not found.")
    record = requests.get(request_id)
    if not isinstance(record, dict):
        raise TenantCreateError("Signup request not found.")
    now = utc_now().isoformat()
    record.update(updates)
    record["updated_at"] = now
    requests[request_id] = record
    _write_store(data, settings)
    return _safe_record(record)


def list_signup_requests(settings: Settings) -> list[dict[str, Any]]:
    """Return public signup requests without exposing verification token hashes."""
    data = _read_store(settings)
    requests = data.get("requests")
    if not isinstance(requests, dict):
        return []
    safe_records: list[dict[str, Any]] = []
    for record in requests.values():
        if not isinstance(record, dict):
            continue
        safe_records.append(_safe_record(record))
    return sorted(
        safe_records,
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )


def _safe_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"token_hash"}
    }


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
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {"requests": {}}
    return data if isinstance(data, dict) else {"requests": {}}


def _write_store(data: dict[str, Any], settings: Settings) -> None:
    path = settings.public_signup_requests_path
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".public_signup.", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
