import json

import httpx
import pytest

from app.config import get_settings
from app.zernio import (
    ZernioAPIError,
    ZernioNotConfigured,
    ZernioService,
    build_whatsapp_callback_url,
)


def test_zernio_settings_defaults_and_secret_repr(monkeypatch):
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    monkeypatch.delenv("ZERNIO_API_BASE_URL", raising=False)
    monkeypatch.delenv("UNBOKS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("UNBOKS_ADMIN_API_URL", raising=False)

    settings = get_settings()

    assert settings.zernio_api_key is None
    assert settings.zernio_api_base_url == "https://zernio.com/api/v1"
    assert settings.unboks_public_url == "https://unboks.org"
    assert settings.unboks_admin_api_url == "https://icp.unboks.org/internal/api"
    assert "zernio_api_key" not in repr(settings)


def test_zernio_env_example_documents_required_vars():
    env_example = open(".env.example", encoding="utf-8").read()

    assert "ZERNIO_API_KEY=" in env_example
    assert "ZERNIO_API_BASE_URL=https://zernio.com/api/v1" in env_example
    assert "ZERNIO_WEBHOOK_SECRET=" in env_example
    assert "LATE_API_KEY=" in env_example
    assert "UNBOKS_PUBLIC_URL=https://unboks.org" in env_example
    assert "UNBOKS_ADMIN_API_URL=https://icp.unboks.org/internal/api" in env_example


def test_zernio_service_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    service = ZernioService(settings=get_settings())

    with pytest.raises(ZernioNotConfigured):
        service.get_connect_url(
            platform="whatsapp",
            profile_id="profile_123",
            redirect_url="https://unboks.org/internal/api/connect/whatsapp/callback",
        )


def test_get_connect_url_calls_zernio_without_exposing_key(monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test_secret")
    monkeypatch.setenv("ZERNIO_API_BASE_URL", "https://zernio.example/api/v1/")
    settings = get_settings()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={
                "authUrl": "https://zernio.example/connect/abc",
                "state": "state_123",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = ZernioService(settings=settings, client=client)

    result = service.get_connect_url(
        platform="whatsapp",
        profile_id="profile_123",
        redirect_url="https://unboks.org/internal/api/connect/whatsapp/callback",
    )

    assert result.auth_url == "https://zernio.example/connect/abc"
    assert result.state == "state_123"
    assert seen["authorization"] == "Bearer sk_test_secret"
    assert seen["url"].startswith("https://zernio.example/api/v1/connect/whatsapp?")
    assert "profileId=profile_123" in seen["url"]
    assert "redirect_url=https%3A%2F%2Funboks.org%2Finternal%2Fapi%2Fconnect%2Fwhatsapp%2Fcallback" in seen["url"]
    assert "sk_test_secret" not in result.auth_url


def test_create_profile_posts_safe_body(monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test_secret")
    monkeypatch.setenv("ZERNIO_API_BASE_URL", "https://zernio.example/api/v1")
    settings = get_settings()
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization", "")
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(
            201,
            json={
                "message": "Profile created successfully",
                "profile": {
                    "_id": "profile_123",
                    "name": "Lawyer",
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = ZernioService(settings=settings, client=client)

    profile = service.create_profile(
        name="Lawyer",
        description="Unboks tenant workspace: lawyer",
    )

    assert profile.id == "profile_123"
    assert profile.name == "Lawyer"
    assert seen["method"] == "POST"
    assert seen["url"] == "https://zernio.example/api/v1/profiles"
    assert seen["authorization"] == "Bearer sk_test_secret"
    assert "sk_test_secret" not in str(seen["body"])
    assert json.loads(str(seen["body"])) == {
        "name": "Lawyer",
        "description": "Unboks tenant workspace: lawyer",
    }


def test_zernio_service_summarizes_whatsapp_accounts(monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test_secret")
    settings = get_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/accounts"
        assert request.url.params["platform"] == "whatsapp"
        return httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "_id": "account_1",
                        "platform": "whatsapp",
                        "profileId": {"_id": "profile_1", "name": "Lawyer"},
                        "displayName": "Lawyer WhatsApp",
                        "username": "+599 9 694 5527",
                        "enabled": True,
                        "isActive": True,
                        "platformStatus": "active",
                        "metadata": {
                            "displayPhoneNumber": "+599 9 694 5527",
                            "phoneNumberId": "phone_1",
                            "wabaId": "waba_1",
                        },
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = ZernioService(settings=settings, client=client)

    accounts = service.list_accounts(platform="whatsapp")

    assert len(accounts) == 1
    account = accounts[0]
    assert account.id == "account_1"
    assert account.profile_name == "Lawyer"
    assert account.display_phone_number == "+599 9 694 5527"
    assert account.phone_number_id == "phone_1"
    assert account.waba_id == "waba_1"
    assert account.is_active is True


def test_zernio_service_deletes_account_and_profile(monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test_secret")
    monkeypatch.setenv("ZERNIO_API_BASE_URL", "https://zernio.example/api/v1")
    settings = get_settings()
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((
            request.method,
            str(request.url),
            request.headers.get("authorization", ""),
        ))
        return httpx.Response(200, json={"success": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = ZernioService(settings=settings, client=client)

    assert service.delete_account("account_1") == {"success": True}
    assert service.delete_profile("profile_1") == {"success": True}

    assert seen == [
        (
            "DELETE",
            "https://zernio.example/api/v1/accounts/account_1",
            "Bearer sk_test_secret",
        ),
        (
            "DELETE",
            "https://zernio.example/api/v1/profiles/profile_1",
            "Bearer sk_test_secret",
        ),
    ]


def test_zernio_service_raises_safe_api_error(monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test_secret")
    settings = get_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = ZernioService(settings=settings, client=client)

    with pytest.raises(ZernioAPIError) as exc:
        service.list_accounts()

    assert exc.value.status_code == 401
    assert exc.value.message == "Unauthorized"
    assert "sk_test_secret" not in str(exc.value)


def test_build_whatsapp_callback_url(monkeypatch):
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org/")

    assert (
        build_whatsapp_callback_url(get_settings())
        == "https://icp.unboks.org/internal/api/connect/whatsapp/callback"
    )
