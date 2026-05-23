import httpx

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
