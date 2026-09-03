"""Backend-only Zernio API service.

This module is intentionally not imported by templates or static JS. It owns
all Zernio API calls so the API key never crosses into browser-rendered code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings, get_settings


class ZernioNotConfigured(RuntimeError):
    """Raised when a backend flow needs Zernio but no API key is configured."""


class ZernioAPIError(RuntimeError):
    """Raised for non-2xx responses from Zernio."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class ZernioConnectUrl:
    auth_url: str
    state: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ZernioProfile:
    id: str
    name: str
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ZernioAccountSummary:
    id: str
    platform: str
    profile_id: str | None
    profile_name: str | None
    display_name: str | None
    username: str | None
    enabled: bool
    is_active: bool
    platform_status: str | None
    display_phone_number: str | None
    phone_number_id: str | None
    waba_id: str | None


class ZernioService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.settings.zernio_api_key)

    def require_configured(self) -> None:
        if not self.configured:
            raise ZernioNotConfigured("ZERNIO_API_KEY is not configured.")

    def get_connect_url(
        self,
        *,
        platform: str,
        profile_id: str,
        redirect_url: str,
        headless: bool = False,
    ) -> ZernioConnectUrl:
        params: dict[str, str] = {
            "profileId": profile_id,
            "redirect_url": redirect_url,
        }
        if headless:
            params["headless"] = "true"
        payload = self._request(
            "GET",
            f"/connect/{platform}",
            query=params,
        )
        auth_url = _first_string(payload, "authUrl", "auth_url", "url")
        if not auth_url:
            raise ZernioAPIError(502, "Zernio did not return an auth URL.")
        return ZernioConnectUrl(
            auth_url=auth_url,
            state=_first_string(payload, "state"),
            raw=payload,
        )

    def create_profile(
        self,
        *,
        name: str,
        description: str | None = None,
        color: str | None = None,
    ) -> ZernioProfile:
        body: dict[str, str] = {"name": name}
        if description:
            body["description"] = description
        if color:
            body["color"] = color
        payload = self._request("POST", "/profiles", json_body=body)
        profile = payload.get("profile")
        if not isinstance(profile, dict):
            profile = payload
        profile_id = _first_string(profile, "_id", "id") if isinstance(profile, dict) else None
        profile_name = _first_string(profile, "name") if isinstance(profile, dict) else None
        if not profile_id:
            raise ZernioAPIError(502, "Zernio did not return a profile id.")
        return ZernioProfile(
            id=profile_id,
            name=profile_name or name,
            raw=profile if isinstance(profile, dict) else payload,
        )

    def list_profiles(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/profiles")
        profiles = payload.get("profiles")
        if isinstance(profiles, list):
            return [p for p in profiles if isinstance(p, dict)]
        data = payload.get("data")
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict)]
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        return []

    def list_accounts(self, *, platform: str | None = None) -> list[ZernioAccountSummary]:
        query = {"platform": platform} if platform else None
        payload = self._request("GET", "/accounts", query=query)
        accounts = payload.get("accounts")
        if not isinstance(accounts, list):
            data = payload.get("data")
            accounts = data if isinstance(data, list) else []
        return [
            summarize_account(account)
            for account in accounts
            if isinstance(account, dict)
        ]

    def get_account(self, account_id: str) -> ZernioAccountSummary:
        payload = self._request("GET", f"/accounts/{account_id}")
        account = payload.get("account") if isinstance(payload, dict) else None
        if not isinstance(account, dict):
            account = payload
        if not isinstance(account, dict):
            raise ZernioAPIError(502, "Zernio returned an invalid account payload.")
        return summarize_account(account)

    def delete_account(self, account_id: str) -> dict[str, Any]:
        """Disconnect a connected provider account from Zernio.

        Zernio documents this as "Disconnect account". It is intentionally
        named delete_account to match the provider API and make tenant wipes
        remove billable external account state, not only local Nr3 rows.
        """
        clean_id = (account_id or "").strip()
        if not clean_id:
            raise ZernioAPIError(400, "Zernio account id is required.")
        return self._request("DELETE", f"/accounts/{clean_id}")

    def delete_profile(self, profile_id: str) -> dict[str, Any]:
        clean_id = (profile_id or "").strip()
        if not clean_id:
            raise ZernioAPIError(400, "Zernio profile id is required.")
        return self._request("DELETE", f"/profiles/{clean_id}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.require_configured()
        url = _join_url(self.settings.zernio_api_base_url, path)
        if query:
            url = f"{url}?{urlencode(query)}"
        client = self._client or httpx.Client(timeout=15)
        close_client = self._client is None
        try:
            response = client.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {self.settings.zernio_api_key}",
                    "Accept": "application/json",
                },
                json=json_body,
            )
        finally:
            if close_client:
                client.close()
        if response.status_code < 200 or response.status_code >= 300:
            raise ZernioAPIError(response.status_code, _safe_error_message(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ZernioAPIError(502, "Zernio returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ZernioAPIError(502, "Zernio returned an invalid JSON payload.")
        return payload


def build_whatsapp_callback_url(
    settings: Settings | None = None,
    *,
    correlation_token: str | None = None,
) -> str:
    resolved = settings or get_settings()
    callback_url = f"{resolved.base_url}/internal/api/connect/whatsapp/callback"
    token = str(correlation_token or "").strip()
    if token:
        callback_url = f"{callback_url}?{urlencode({'nr3_token': token})}"
    return callback_url


def summarize_account(account: dict[str, Any]) -> ZernioAccountSummary:
    profile = account.get("profileId")
    metadata = account.get("metadata")
    if not isinstance(profile, dict):
        profile = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return ZernioAccountSummary(
        id=str(account.get("_id") or account.get("id") or ""),
        platform=str(account.get("platform") or ""),
        profile_id=_string_or_none(profile.get("_id") or profile.get("id")),
        profile_name=_string_or_none(profile.get("name")),
        display_name=_string_or_none(account.get("displayName")),
        username=_string_or_none(account.get("username")),
        enabled=bool(account.get("enabled")),
        is_active=bool(account.get("isActive")),
        platform_status=_string_or_none(account.get("platformStatus")),
        display_phone_number=_string_or_none(metadata.get("displayPhoneNumber")),
        phone_number_id=_string_or_none(metadata.get("phoneNumberId")),
        waba_id=_string_or_none(metadata.get("wabaId")),
    )


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Zernio request failed with HTTP {response.status_code}."
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"Zernio request failed with HTTP {response.status_code}."
