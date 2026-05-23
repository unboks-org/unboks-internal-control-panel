import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.nr2_sync import Nr2KnowledgeSync
from app.nr2_sync import fetch_nr2_knowledge


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
        if request.url.path.endswith("/knowledge/media"):
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
        if request.url.path.endswith("/knowledge/media"):
            return httpx.Response(404)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    sync = fetch_nr2_knowledge("test", client=client)

    assert sync.status == "partial"
    assert "/knowledge/media missing" in sync.error


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
        ),
    )
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.get("/admin/tenants/unboks")

    assert response.status_code == 200
    assert "Nr2 company knowledge" in response.text
    assert "Oceanview Apartment" in response.text
    assert "Ask discovery questions first." in response.text
