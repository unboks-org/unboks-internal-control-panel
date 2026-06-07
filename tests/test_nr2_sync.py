import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.nr2_sync import Nr2KnowledgeSync
from app.nr2_sync import (
    delete_nr2_photo,
    fetch_nr2_knowledge,
    fetch_nr2_photo_image,
    update_nr2_info_update,
    upload_nr2_photo,
)


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
