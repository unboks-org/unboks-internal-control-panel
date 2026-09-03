import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app import nr2_sync
from app.delete_operations import DeleteOperationConflict
from app.main import app
from app.nr2_sync import Nr2KnowledgeSync
from app.nr2_sync import (
    delete_nr2_photo,
    fetch_nr2_knowledge,
    fetch_nr2_photo_image,
    update_nr2_product_settings,
    update_nr2_info_update,
    upload_nr2_photo,
)


def _invoke_nr2_outbound_mutation(
    operation: str,
    *,
    tenant_id: str,
    client: httpx.Client,
    expected_generation_id: str | None = None,
):
    common = {
        "client": client,
        "expected_generation_id": expected_generation_id,
    }
    if operation == "info_update":
        return update_nr2_info_update(
            tenant_id,
            "42",
            type_="general",
            text="The Monday departure is confirmed.",
            **common,
        )
    if operation == "photo_upload":
        return upload_nr2_photo(
            tenant_id,
            filename="klein-curacao.jpg",
            content_type="image/jpeg",
            content=b"JPEGDATA",
            **common,
        )
    if operation == "photo_delete":
        return delete_nr2_photo(tenant_id, "7", **common)
    if operation == "auto_block":
        return nr2_sync.update_auto_block_settings(
            tenant_id,
            {"enabled": True},
            **common,
        )
    if operation == "product_settings":
        return update_nr2_product_settings(
            tenant_id,
            delivery_cost_amount="7",
            delivery_cost_currency="XCG",
            **common,
        )
    raise AssertionError(f"Unknown test operation: {operation}")


def test_nr2_sync_missing_credentials(monkeypatch):
    monkeypatch.setattr("app.nr2_sync.get_tenant_client_data", lambda tenant: {})

    sync = fetch_nr2_knowledge("lawyer")

    assert sync.status == "missing_credentials"
    assert sync.total_items == 0
    assert "password" in sync.error


def test_nr2_sync_fetches_safe_company_knowledge(monkeypatch):
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: {"password": "secret-password"},
    )
    monkeypatch.setenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "https://api.example.test/api/{tenant}/dashboard/api",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.example.test"
        if request.method == "POST" and request.url.path.endswith("/login"):
            assert request.content == b'{"password":"secret-password"}'
            return httpx.Response(200, json={"token": "safe-token"})
        assert request.headers["authorization"] == "Bearer safe-token"
        if request.url.path.endswith("/source-of-truth"):
            return httpx.Response(
                200,
                json={
                    "blocks": [
                        {
                            "title": "Properties",
                            "content": "Five active listings.",
                            "items": ["Oceanview Apartment", "Blue Bay Villa"],
                        }
                    ]
                },
            )
        if request.url.path.endswith("/settings/info-updates"):
            return httpx.Response(
                200,
                json={
                    "updates": [
                        {
                            "id": "u1",
                            "type": "general",
                            "text": "Ask discovery questions before offering viewings.",
                            "active": True,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/knowledge/files"):
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "filename": "property-list.pdf",
                            "status": "ready",
                            "sizeBytes": 1200,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/knowledge/media/library"):
            return httpx.Response(
                200,
                json={
                    "media": [
                        {
                            "caption": "Oceanview balcony",
                            "url": "https://dashboard.unboks.org/media/ocean.jpg",
                            "knowledgeId": "k1",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/settings/product-settings"):
            return httpx.Response(
                200,
                json={
                    "delivery_cost_amount": 5,
                    "delivery_cost_currency": "XCG",
                },
            )
        if request.url.path.endswith("/runtime-prompt-manifest"):
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "sources": [
                        {
                            "id": "runtime.marina.whatsapp.system",
                            "name": "Live Marina WhatsApp system prompt",
                            "source_location": "wtyj/agents/marina/marina_agent.py",
                            "used_in": ["whatsapp"],
                            "prompt_kind": "system",
                            "priority": "platform_safety",
                            "status": "indexed",
                            "text": "Never tell jokes. Password: should-not-leak",
                        }
                    ],
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    sync = fetch_nr2_knowledge("lawyer", client=client)

    assert sync.status == "ok"
    assert sync.source_url == "https://api.example.test/api/lawyer/dashboard/api"
    assert sync.total_items == 4
    assert sync.sot_blocks[0]["title"] == "Properties"
    assert sync.info_updates[0]["text"] == "Ask discovery questions before offering viewings."
    assert sync.knowledge_files[0]["filename"] == "property-list.pdf"
    assert sync.knowledge_media[0]["caption"] == "Oceanview balcony"
    assert sync.product_settings == {
        "delivery_cost_amount": 5.0,
        "delivery_cost_currency": "XCG",
    }
    assert sync.runtime_prompt_manifest["sources"][0]["id"] == "runtime.marina.whatsapp.system"
    assert "should-not-leak" not in sync.runtime_prompt_manifest["sources"][0]["text"]
    assert "[REDACTED]" in sync.runtime_prompt_manifest["sources"][0]["text"]


def test_nr2_sync_updates_info_update(monkeypatch):
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: {"password": "secret-password"},
    )
    monkeypatch.setenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "https://api.example.test/api/{tenant}/dashboard/api",
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "safe-token"})
        assert request.headers["authorization"] == "Bearer safe-token"
        if request.method == "PUT" and request.url.path.endswith("/settings/info-updates/42"):
            return httpx.Response(200, json={"ok": True, "id": 42})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = update_nr2_info_update(
        "wibrandt",
        "42",
        type_="pricing",
        text="New cupcake price is 7 XCG.",
        active=True,
        start_date="",
        end_date="",
        client=client,
    )

    assert result.ok
    assert calls[0][0] == "POST"
    assert calls[1][0] == "PUT"
    assert calls[1][1].endswith("/settings/info-updates/42")
    assert b'"text":"New cupcake price is 7 XCG."' in calls[1][2]


def test_nr2_sync_updates_product_settings(monkeypatch):
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: {"password": "secret-password"},
    )
    monkeypatch.setenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "https://api.example.test/api/{tenant}/dashboard/api",
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "safe-token"})
        assert request.headers["authorization"] == "Bearer safe-token"
        if request.method == "PUT" and request.url.path.endswith("/settings/product-settings"):
            return httpx.Response(
                200,
                json={
                    "delivery_cost_amount": 7,
                    "delivery_cost_currency": "XCG",
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = update_nr2_product_settings(
        "wibrandt",
        delivery_cost_amount="7",
        delivery_cost_currency="xcg",
        client=client,
    )

    assert result.ok
    assert result.settings == {
        "delivery_cost_amount": 7.0,
        "delivery_cost_currency": "XCG",
    }
    assert calls[0][0] == "POST"
    assert calls[1][0] == "PUT"
    assert calls[1][1].endswith("/settings/product-settings")
    assert b'"delivery_cost_amount":7.0' in calls[1][2]


def test_nr2_sync_handles_optional_missing_endpoint_as_partial(monkeypatch):
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: {"access_key": "access-key"},
    )
    monkeypatch.setenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "https://api.example.test/api/{tenant}/dashboard/api",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "safe-token"})
        if request.url.path.endswith("/knowledge/media/library") or request.url.path.endswith("/photos"):
            return httpx.Response(404)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    sync = fetch_nr2_knowledge("test", client=client)

    assert sync.status == "partial"
    assert "/photos missing" in sync.error


def test_nr2_sync_falls_back_to_photo_library_for_images(monkeypatch):
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: {"dashboard_password": "dash-password"},
    )
    monkeypatch.setenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "https://api.example.test/api/{tenant}/dashboard/api",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "safe-token"})
        if request.url.path.endswith("/knowledge/media/library"):
            return httpx.Response(404)
        if request.url.path.endswith("/photos"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "filename": "photo_42.jpg",
                        "original_filename": "Cupcake.jpg",
                        "tags": ["cupcake", "red velvet"],
                        "service_key": "red-velvet",
                        "uploaded_at": "2026-06-05T00:00:00+00:00",
                    }
                ],
            )
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    sync = fetch_nr2_knowledge("wibrandt", client=client)

    assert sync.knowledge_media[0]["caption"] == "Cupcake.jpg"
    assert sync.knowledge_media[0]["id"] == "42"
    assert sync.knowledge_media[0]["tags"] == "cupcake, red velvet"
    assert sync.knowledge_media[0]["service_key"] == "red-velvet"
    assert sync.knowledge_media[0]["url"] == "/admin/tenants/wibrandt/media/42"
    assert sync.knowledge_media[0]["provider_url"] == (
        "https://api.unboks.org/api/wibrandt/dashboard/api/public/media/photo_42.jpg"
    )


def test_upload_nr2_photo_posts_to_tenant_photo_library(monkeypatch):
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: {"password": "secret-password"},
    )
    monkeypatch.setenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "https://api.example.test/api/{tenant}/dashboard/api",
    )
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "safe-token"})
        assert request.headers["authorization"] == "Bearer safe-token"
        if request.method == "POST" and request.url.path.endswith("/photos/upload"):
            body = request.content.decode("latin1")
            seen["body"] = body
            return httpx.Response(200, json={"ok": True, "photo": {"id": 7}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = upload_nr2_photo(
        "wibrandt",
        filename="product.png",
        content_type="image/png",
        content=b"PNGDATA",
        tags="product, red",
        service_key="product-red",
        client=client,
    )

    assert result.ok is True
    assert result.photo["id"] == 7
    assert "product.png" in seen["body"]
    assert "product, red" in seen["body"]
    assert "product-red" in seen["body"]


def test_fetch_nr2_photo_image_proxies_authenticated_image(monkeypatch):
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: {"password": "secret-password"},
    )
    monkeypatch.setenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "https://api.example.test/api/{tenant}/dashboard/api",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "safe-token"})
        assert request.headers["authorization"] == "Bearer safe-token"
        if request.url.path.endswith("/photos/7/image"):
            return httpx.Response(200, content=b"JPEGDATA", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    content, content_type, error = fetch_nr2_photo_image("wibrandt", "7", client=client)

    assert error == ""
    assert content == b"JPEGDATA"
    assert content_type == "image/jpeg"


def test_delete_nr2_photo_deletes_from_tenant_photo_library(monkeypatch):
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: {"password": "secret-password"},
    )
    monkeypatch.setenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "https://api.example.test/api/{tenant}/dashboard/api",
    )
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "safe-token"})
        assert request.headers["authorization"] == "Bearer safe-token"
        if request.method == "DELETE" and request.url.path.endswith("/photos/7"):
            seen["deleted"] = request.url.path
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = delete_nr2_photo("wibrandt", "7", client=client)

    assert result.ok is True
    assert seen["deleted"] == "/api/wibrandt/dashboard/api/photos/7"


@pytest.mark.parametrize(
    "operation",
    (
        "info_update",
        "photo_upload",
        "photo_delete",
        "auto_block",
        "product_settings",
    ),
)
def test_nr2_outbound_mutators_reject_stale_generation_before_credentials_or_http(
    monkeypatch,
    operation,
):
    tenant_id = f"mermaid-{operation.replace('_', '-')}"
    current_generation = nr2_sync._capture_tenant_generation(tenant_id)
    assert current_generation != "stale-generation"

    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: (_ for _ in ()).throw(
            AssertionError("stale mutation must not read replacement credentials")
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"stale mutation must not make an Nr2 request: {request.method} {request.url}"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(DeleteOperationConflict, match="generation changed"):
        _invoke_nr2_outbound_mutation(
            operation,
            tenant_id=tenant_id,
            client=client,
            expected_generation_id="stale-generation",
        )


@pytest.mark.parametrize(
    "operation",
    (
        "info_update",
        "photo_upload",
        "photo_delete",
        "auto_block",
        "product_settings",
    ),
)
def test_nr2_outbound_mutators_hold_lifecycle_lease_through_credentials_and_http(
    monkeypatch,
    operation,
):
    from app.provisioning import tenant_creation_lock

    tenant_id = f"tracy-{operation.replace('_', '-')}"
    current_generation = nr2_sync._capture_tenant_generation(tenant_id)
    contender_attempting = threading.Event()
    contender_acquired = threading.Event()

    def contend_for_lifecycle_lease() -> None:
        contender_attempting.set()
        with tenant_creation_lock(tenant_id):
            contender_acquired.set()

    contender: threading.Thread | None = None

    def credentials(tenant: str) -> dict[str, str]:
        nonlocal contender
        assert tenant == tenant_id
        contender = threading.Thread(target=contend_for_lifecycle_lease)
        contender.start()
        assert contender_attempting.wait(1)
        assert not contender_acquired.wait(0.05)
        return {"password": "generation-bound-password"}

    monkeypatch.setattr("app.nr2_sync.get_tenant_client_data", credentials)
    mutation_requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/login"):
            assert request.content == b'{"password":"generation-bound-password"}'
            return httpx.Response(200, json={"token": "safe-token"})
        assert contender_attempting.is_set()
        assert not contender_acquired.is_set()
        assert request.headers["authorization"] == "Bearer safe-token"
        mutation_requests.append((request.method, request.url.path))
        if request.url.path.endswith("/settings/product-settings"):
            return httpx.Response(
                200,
                json={
                    "delivery_cost_amount": 7,
                    "delivery_cost_currency": "XCG",
                },
            )
        if request.url.path.endswith("/photos/upload"):
            return httpx.Response(200, json={"photo": {"id": 7}})
        return httpx.Response(200, json={"enabled": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = _invoke_nr2_outbound_mutation(
        operation,
        tenant_id=tenant_id,
        client=client,
        expected_generation_id=current_generation,
    )

    assert result.ok is True
    assert len(mutation_requests) == 1
    assert contender is not None
    contender.join(timeout=2)
    assert not contender.is_alive()
    assert contender_acquired.is_set()


def test_nr2_sync_returns_fresh_cache_without_hitting_runtime(monkeypatch, tmp_path):
    cache = tmp_path / "nr2_cache.json"
    cache.write_text(
        """
{
  "lawyer": {
    "status": "ok",
    "source_url": "https://api.example.test/api/lawyer/dashboard/api",
    "error": "",
    "fetched_at": "2999-01-01T00:00:00+00:00",
    "sot_blocks": [{"title": "Cached SOT", "content": "Cached text."}],
    "info_updates": [],
    "knowledge_files": [],
    "knowledge_media": []
  }
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_NR2_KNOWLEDGE_CACHE_PATH", str(cache))
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: (_ for _ in ()).throw(AssertionError("runtime should not be called")),
    )

    sync = fetch_nr2_knowledge("lawyer")

    assert sync.cached is True
    assert sync.sot_blocks[0]["title"] == "Cached SOT"


def test_nr2_cache_forget_tenant_removes_exact_slug_and_preserves_others(
    monkeypatch,
    tmp_path,
):
    cache = tmp_path / "nr2_cache.json"
    cache.write_text(
        json.dumps(
            {
                "mermaid": {"status": "ok", "sot_blocks": [{"title": "Trip"}]},
                "mermaid-demo": {"status": "ok", "sot_blocks": [{"title": "Demo"}]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_NR2_KNOWLEDGE_CACHE_PATH", str(cache))

    assert nr2_sync.tenant_state_exists("mermaid") is True
    assert nr2_sync.forget_tenant("mermaid") is True
    assert nr2_sync.tenant_state_exists("mermaid") is False
    assert nr2_sync.forget_tenant("mermaid") is False

    remaining = json.loads(cache.read_text(encoding="utf-8"))
    assert remaining == {
        "mermaid-demo": {"status": "ok", "sot_blocks": [{"title": "Demo"}]}
    }


def test_nr2_cache_cleanup_and_absence_checks_fail_closed_on_malformed_store(
    monkeypatch,
    tmp_path,
):
    cache = tmp_path / "nr2_cache.json"
    cache.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("NR3_NR2_KNOWLEDGE_CACHE_PATH", str(cache))

    with pytest.raises(RuntimeError, match="unreadable"):
        nr2_sync.tenant_state_exists("mermaid")
    with pytest.raises(RuntimeError, match="unreadable"):
        nr2_sync.forget_tenant("mermaid")

    assert cache.read_text(encoding="utf-8") == "{broken"


def test_nr2_cache_absence_check_fails_closed_on_unreadable_store(
    monkeypatch,
    tmp_path,
):
    cache = tmp_path / "nr2_cache.json"
    cache.mkdir()
    monkeypatch.setenv("NR3_NR2_KNOWLEDGE_CACHE_PATH", str(cache))

    with pytest.raises(RuntimeError, match="unreadable"):
        nr2_sync.tenant_state_exists("mermaid")


def test_nr2_cache_write_rejects_stale_tenant_generation(monkeypatch, tmp_path):
    cache = tmp_path / "nr2_cache.json"
    monkeypatch.setenv("NR3_NR2_KNOWLEDGE_CACHE_PATH", str(cache))
    current = Nr2KnowledgeSync(
        status="ok",
        fetched_at="2026-09-02T12:00:00+00:00",
        sot_blocks=({"title": "Current generation"},),
    )
    stale = Nr2KnowledgeSync(
        status="ok",
        fetched_at="2026-09-02T12:01:00+00:00",
        sot_blocks=({"title": "Stale generation"},),
    )
    nr2_sync._write_tenant_cache("mermaid", current)
    before = cache.read_text(encoding="utf-8")

    with pytest.raises(DeleteOperationConflict, match="generation changed"):
        nr2_sync._write_tenant_cache(
            "mermaid",
            stale,
            expected_generation_id="stale-generation",
        )

    assert cache.read_text(encoding="utf-8") == before


def test_nr2_sync_refresh_bypasses_cache(monkeypatch, tmp_path):
    cache = tmp_path / "nr2_cache.json"
    cache.write_text(
        """
{
  "lawyer": {
    "status": "ok",
    "source_url": "cached",
    "fetched_at": "2999-01-01T00:00:00+00:00",
    "sot_blocks": [{"title": "Old", "content": "Old"}]
  }
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_NR2_KNOWLEDGE_CACHE_PATH", str(cache))
    monkeypatch.setattr(
        "app.nr2_sync.get_tenant_client_data",
        lambda tenant: {"password": "secret-password"},
    )
    monkeypatch.setenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "https://api.example.test/api/{tenant}/dashboard/api",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "safe-token"})
        if request.url.path.endswith("/source-of-truth"):
            return httpx.Response(200, json={"blocks": [{"title": "Fresh"}]})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    sync = fetch_nr2_knowledge("lawyer", client=client, refresh=True)

    assert sync.cached is False
    assert sync.sot_blocks[0]["title"] == "Fresh"


def test_workspace_renders_synced_sot_items_without_jinja_dict_collision(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp.json"))
    (tmp_path / "tenants").mkdir()
    monkeypatch.setattr(
        "app.routes.admin.fetch_nr2_knowledge",
        lambda tenant_id: Nr2KnowledgeSync(
            status="ok",
            source_url="https://api.unboks.org/api/test/dashboard/api",
            fetched_at="2026-05-25T00:00:00+00:00",
            cached=True,
            sot_blocks=(
                {
                    "title": "Listings",
                    "content": "Real estate inventory.",
                    "items": ("Oceanview Apartment", "Blue Bay Villa"),
                    "subsections": (
                        {"title": "Rules", "content": "Ask discovery questions first."},
                    ),
                },
            ),
            info_updates=(
                {
                    "id": "12",
                    "type": "general",
                    "text": "Ask discovery questions first.",
                    "active": True,
                },
            ),
        ),
    )
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.get("/admin/tenants/unboks")

    assert response.status_code == 200
    assert "Nr2 company knowledge" in response.text
    assert "Refresh from Nr2" in response.text
    assert "(cached)" in response.text
    assert "Oceanview Apartment" in response.text
    assert "Ask discovery questions first." in response.text
    assert "/admin/tenants/unboks/nr2-knowledge/info-updates/12/edit" in response.text
    assert "Save edit in Nr2" in response.text


def test_workspace_refresh_route_forces_nr2_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp.json"))
    (tmp_path / "tenants").mkdir()

    calls = []

    def fake_fetch(tenant_id: str, *, refresh: bool = False):
        calls.append((tenant_id, refresh))
        return Nr2KnowledgeSync(status="ok")

    monkeypatch.setattr("app.routes.admin.fetch_nr2_knowledge", fake_fetch)
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})
    calls.clear()

    response = client.post(
        "/admin/tenants/unboks/nr2-knowledge/refresh",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert calls == [("unboks", True)]
    assert "action_message=Nr2+company+knowledge+refreshed." in response.headers["location"]
