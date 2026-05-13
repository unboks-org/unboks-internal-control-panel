from fastapi.testclient import TestClient

from app.main import app
from app.onboarding import (
    INTAKE_QUESTIONS,
    LeadInput,
    create_lead,
    create_or_refresh_token,
    get_lead,
    hash_token,
    list_intake_answers,
)


def test_healthz_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "wtyj-admin"}


def test_admin_redirects_unauthenticated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)

    response = client.get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_create_onboarding_lead_persists_and_rejects_duplicate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)

    login = client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    created = client.post(
        "/admin/onboarding/leads",
        data={
            "email": "test@example.com",
            "business_name": "Test Business",
            "contact_name": "Test Contact",
            "language": "English",
            "notes": "Internal note",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/admin/onboarding"

    onboarding = client.get("/admin/onboarding")
    assert onboarding.status_code == 200
    assert "test@example.com" in onboarding.text
    assert "Test Business" in onboarding.text
    assert "lead_created" in onboarding.text

    home = client.get("/admin")
    assert home.status_code == 200
    # Home is a compact operational snapshot; lead total should be at least 1
    assert "Onboarding leads" in home.text

    duplicate = client.post(
        "/admin/onboarding/leads",
        data={"email": "TEST@example.com"},
    )
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.text

    refreshed = TestClient(app)
    refreshed.cookies.update(client.cookies)
    persisted = refreshed.get("/admin/onboarding")
    assert persisted.status_code == 200
    assert "test@example.com" in persisted.text


def test_admin_shell_renders_tenant_first_sidebar(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    home = client.get("/admin")
    assert home.status_code == 200
    assert "app-shell" in home.text
    assert "tenant-selector" in home.text
    # Tenants button label must be exactly "Tenants" — no "Select tenant" placeholder
    assert "tenant-selector-label" in home.text
    assert "Select tenant" not in home.text
    assert "Unboks Demo" in home.text
    assert "Consulta Despertares" in home.text
    assert "BlueFinn Charters" in home.text
    # Sidebar must only show TENANTS, HOME, SETTINGS — not Onboarding/Reviews
    assert "sidebar-nav" in home.text
    assert ">Home<" in home.text
    assert ">Settings<" in home.text
    # Sidebar must not contain Onboarding/Reviews as nav-items (they live in body links only)
    sidebar = home.text.split('class="sidebar-nav"', 1)[1].split("</nav>", 1)[0]
    assert "Onboarding" not in sidebar
    assert "Reviews" not in sidebar
    assert "Home" in sidebar
    assert "Settings" in sidebar
    # Active state on Home
    assert 'aria-current="page"' in home.text

    settings_page = client.get("/admin/settings")
    assert settings_page.status_code == 200
    assert "tenant-selector" in settings_page.text
    assert "sidebar-nav" in settings_page.text


def test_tenant_workspace_renders_with_status_and_actions(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    workspace = client.get("/admin/tenants/unboks-demo")
    assert workspace.status_code == 200
    assert "Unboks Demo" in workspace.text
    # Soft-colored tenant header
    assert "tenant-header" in workspace.text
    # Compact health strip with all 6 cells
    assert "health-strip" in workspace.text
    for label in ("Inbox", "AI Agent", "Channels", "Source of Truth", "Escalations", "Billing / Trial"):
        assert label in workspace.text
    # Primary controls
    assert "Open tenant dashboard" in workspace.text
    assert "Push changes" in workspace.text
    assert "Edit tenant" in workspace.text
    assert "Pause tenant" in workspace.text
    # Operational cards
    assert "op-grid" in workspace.text
    # AI Agent control panel
    assert "agent-panel" in workspace.text
    assert "Agent replies" in workspace.text
    assert "Auto-reply" in workspace.text
    assert "Human takeover" in workspace.text
    assert "Learning from operator answers" in workspace.text
    assert "Escalation behavior" in workspace.text
    assert "Soft escalation allowed" in workspace.text
    assert "Hard escalation allowed" in workspace.text
    assert "Both allowed" in workspace.text
    assert "Tone / personality" in workspace.text
    assert "Edit tone" in workspace.text
    assert "Escalation rules" in workspace.text
    assert "Edit rules" in workspace.text
    assert "Test Agent reply" in workspace.text
    # Each toggle must be a real disabled control, not a passive chip
    assert workspace.text.count('class="agent-toggle"') >= 4
    # Forbidden legacy terminology
    assert "Soft mode" not in workspace.text
    assert "Hard mode" not in workspace.text
    # Billing / Trial panel
    assert "billing-panel" in workspace.text
    assert "Billing / Trial" in workspace.text
    assert "Trial days left" in workspace.text
    assert "Next billing" in workspace.text
    assert "Monthly price" in workspace.text
    for action in ("Extend trial", "Mark paid", "Pause billing", "Cancel tenant"):
        assert action in workspace.text
    # Cancel tenant must be visually separated in a danger zone
    assert "billing-danger" in workspace.text
    # Activity log / audit trail
    assert "activity-panel" in workspace.text
    assert "View full activity log" in workspace.text
    assert "Export log" in workspace.text
    # No fake events: empty tenants must show "No activity yet"
    assert "No activity yet" in workspace.text
    # Source of Truth / Data Room
    assert "data-room" in workspace.text
    assert "Knowledge items" in workspace.text
    assert "Cloud connection" in workspace.text
    assert "Last sync" in workspace.text
    assert "Pending review" in workspace.text
    for action in ("Upload files", "Connect cloud directory", "View knowledge items", "Sync now"):
        assert action in workspace.text
    for provider in ("Google Drive", "Dropbox", "OneDrive"):
        assert provider in workspace.text
    for category in ("Documents / PDFs", "Images", "Price lists", "Menus / brochures",
                     "FAQ files", "Policies", "Services / product sheets"):
        assert category in workspace.text
    # Activity log shows empty state
    assert "Activity" in workspace.text
    # Danger zone
    assert "danger-zone" in workspace.text
    assert "Suspend / cut off tenant" in workspace.text
    # All controls are still placeholders, must render disabled
    assert "disabled" in workspace.text
    assert 'aria-current="page"' in workspace.text


def test_tenant_workspace_shows_no_activity_for_empty_tenant(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    workspace = client.get("/admin/tenants/consulta-despertares")
    assert workspace.status_code == 200
    assert "No activity yet" in workspace.text
    # Empty tenant must show "No recent replies yet" inside the AI Agent panel
    assert "No recent replies yet" in workspace.text

    # /admin/tenants redirects to first tenant
    index = client.get("/admin/tenants", follow_redirects=False)
    assert index.status_code == 303
    assert index.headers["location"] == "/admin/tenants/unboks-demo"

    # Unknown tenant returns 404 but still renders shell
    missing = client.get("/admin/tenants/no-such-tenant")
    assert missing.status_code == 404


def test_admin_onboarding_and_reviews_still_reachable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    onboarding = client.get("/admin/onboarding")
    assert onboarding.status_code == 200
    reviews = client.get("/admin/reviews")
    assert reviews.status_code == 200
    assert "Awaiting review" in reviews.text


def test_public_onboarding_does_not_render_admin_shell(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    lead = create_lead(
        LeadInput(
            email="public@example.com",
            business_name=None,
            contact_name=None,
            language=None,
            notes=None,
        )
    )
    _, token = create_or_refresh_token(lead.id)
    client = TestClient(app)

    page = client.get(f"/onboarding/{token}")
    assert page.status_code == 200
    assert "sidebar-nav" not in page.text
    assert "app-shell" not in page.text

    invalid = client.get("/onboarding/not-a-valid-token")
    assert invalid.status_code == 404
    assert "sidebar-nav" not in invalid.text

    login = client.get("/login")
    assert login.status_code == 200
    assert "sidebar-nav" not in login.text


def test_admin_send_email_route_requires_auth(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)

    response = client.post(
        "/admin/onboarding/leads/1/send-email",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_lead_review_requires_auth(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)

    response = client.get("/admin/onboarding/leads/1", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_token_generation_and_public_onboarding_placeholder(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_BASE_URL", "http://testserver")
    lead = create_lead(
        LeadInput(
            email="token@example.com",
            business_name="Token Business",
            contact_name=None,
            language=None,
            notes=None,
        )
    )

    updated, token = create_or_refresh_token(lead.id)

    assert token
    assert updated.onboarding_token_hash == hash_token(token)
    assert token not in updated.onboarding_token_hash

    client = TestClient(app)
    valid = client.get(f"/onboarding/{token}")
    assert valid.status_code == 200
    assert "Business intake" in valid.text
    assert "Step 1 of" in valid.text

    invalid = client.get("/onboarding/not-a-valid-token")
    assert invalid.status_code == 404
    assert "invalid or expired" in invalid.text


def test_missing_smtp_generates_preview_without_fake_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_BASE_URL", "http://testserver")
    monkeypatch.delenv("NR3_SMTP_HOST", raising=False)
    monkeypatch.delenv("NR3_SMTP_USERNAME", raising=False)
    monkeypatch.delenv("NR3_SMTP_PASSWORD", raising=False)
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})
    created = client.post(
        "/admin/onboarding/leads",
        data={"email": "manual@example.com"},
        follow_redirects=False,
    )
    assert created.status_code == 303

    send = client.post("/admin/onboarding/leads/1/send-email")

    assert send.status_code == 200
    assert "Email not configured" in send.text
    assert "Welcome to Unboks" in send.text
    assert "http://testserver/onboarding/" in send.text

    lead = get_lead(1)
    assert lead.status == "email_pending"
    assert lead.email_sent_at is None
    assert lead.email_last_error == "Email not configured."


def test_public_onboarding_saves_one_question_at_a_time(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    lead = create_lead(
        LeadInput(
            email="intake@example.com",
            business_name="Intake Business",
            contact_name=None,
            language=None,
            notes=None,
        )
    )
    _, token = create_or_refresh_token(lead.id)
    client = TestClient(app)

    blank = client.post(
        f"/onboarding/{token}",
        data={"question_key": INTAKE_QUESTIONS[0].key, "answer": "  "},
    )
    assert blank.status_code == 400
    assert "Answer is required" in blank.text

    first = client.post(
        f"/onboarding/{token}",
        data={
            "question_key": INTAKE_QUESTIONS[0].key,
            "answer": "We repair air conditioners.",
        },
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert first.headers["location"] == f"/onboarding/{token}"

    page = client.get(f"/onboarding/{token}")
    assert page.status_code == 200
    assert "Step 2 of" in page.text

    answers = list_intake_answers(lead.id)
    assert answers[INTAKE_QUESTIONS[0].key].answer == "We repair air conditioners."
    assert get_lead(lead.id).status == "form_started"


def test_public_onboarding_completion_updates_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    lead = create_lead(
        LeadInput(
            email="complete@example.com",
            business_name=None,
            contact_name=None,
            language=None,
            notes=None,
        )
    )
    _, token = create_or_refresh_token(lead.id)
    client = TestClient(app)

    for question in INTAKE_QUESTIONS:
        response = client.post(
            f"/onboarding/{token}",
            data={"question_key": question.key, "answer": f"Answer for {question.key}"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    complete = client.get(f"/onboarding/{token}")
    assert complete.status_code == 200
    assert "Onboarding received" in complete.text
    assert "Your answers were saved successfully." in complete.text
    assert "You can close this tab now." in complete.text
    assert "What happens next" in complete.text
    assert get_lead(lead.id).status == "form_submitted"


def test_admin_can_review_and_export_setup_summary(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    lead = create_lead(
        LeadInput(
            email="review@example.com",
            business_name="Review Business",
            contact_name="Review Contact",
            language="English",
            notes="Needs careful setup.",
        )
    )
    _, token = create_or_refresh_token(lead.id)
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    for question in INTAKE_QUESTIONS:
        response = client.post(
            f"/onboarding/{token}",
            data={"question_key": question.key, "answer": f"Answer for {question.key}"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    review = client.get(f"/admin/onboarding/leads/{lead.id}")
    assert review.status_code == 200
    assert "Onboarding review" in review.text
    assert "Review Business" in review.text
    assert "Answer for business_summary" in review.text
    assert "Setup summary" in review.text

    export = client.get(f"/admin/onboarding/leads/{lead.id}/setup-summary.txt")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/plain")
    assert "Unboks onboarding setup summary" in export.text
    assert "Review Business" in export.text
    assert "Answer for escalation_rules" in export.text


def test_admin_can_mark_review_needs_changes_and_approved(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    lead = create_lead(
        LeadInput(
            email="decision@example.com",
            business_name="Decision Business",
            contact_name=None,
            language=None,
            notes=None,
        )
    )
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    needs_changes = client.post(
        f"/admin/onboarding/leads/{lead.id}/review",
        data={
            "decision": "needs_changes",
            "review_notes": "Need better pricing details.",
        },
        follow_redirects=False,
    )
    assert needs_changes.status_code == 303
    updated = get_lead(lead.id)
    assert updated.status == "review_needs_changes"
    assert updated.review_status == "needs_changes"
    assert updated.review_notes == "Need better pricing details."
    assert updated.reviewed_at is not None

    approved = client.post(
        f"/admin/onboarding/leads/{lead.id}/review",
        data={"decision": "approved", "review_notes": "Ready for setup."},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    approved_lead = get_lead(lead.id)
    assert approved_lead.status == "review_approved"
    assert approved_lead.review_status == "approved"
    assert approved_lead.review_notes == "Ready for setup."

    detail = client.get(f"/admin/onboarding/leads/{lead.id}")
    assert detail.status_code == 200
    assert "Ready for setup." in detail.text

    export = client.get(f"/admin/onboarding/leads/{lead.id}/setup-summary.txt")
    assert "Review status: approved" in export.text
    assert "Ready for setup." in export.text


def test_admin_review_rejects_invalid_decision(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    lead = create_lead(
        LeadInput(
            email="invalid-review@example.com",
            business_name=None,
            contact_name=None,
            language=None,
            notes=None,
        )
    )
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.post(
        f"/admin/onboarding/leads/{lead.id}/review",
        data={"decision": "tenant_ready", "review_notes": "No."},
    )

    assert response.status_code == 400
    assert "Invalid review decision" in response.text
    assert get_lead(lead.id).status == "lead_created"
