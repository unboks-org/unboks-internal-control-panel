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
    assert created.headers["location"] == "/admin"

    admin = client.get("/admin")
    assert admin.status_code == 200
    assert "test@example.com" in admin.text
    assert "Test Business" in admin.text
    assert "lead_created" in admin.text

    duplicate = client.post(
        "/admin/onboarding/leads",
        data={"email": "TEST@example.com"},
    )
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.text

    refreshed = TestClient(app)
    refreshed.cookies.update(client.cookies)
    persisted = refreshed.get("/admin")
    assert persisted.status_code == 200
    assert "test@example.com" in persisted.text


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
