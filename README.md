# unboks-internal-control-panel

Internal Unboks control panel and onboarding app.

This repository is for Nr 3 only: the private Unboks.org internal control panel. The current scaffold intentionally contains only:

- FastAPI backend
- Vanilla HTML/CSS/JS frontend
- `/healthz`
- password-protected `/admin`
- local onboarding lead creation/status list
- secure onboarding link generation
- welcome email sending or manual-send preview when SMTP is not configured
- token-gated one-question-at-a-time onboarding intake
- protected admin review of intake answers and text setup summary export
- internal review decision states for approved / needs changes
- Docker service shape for `wtyj-admin` on port `8010`
- nginx IP allowlist template

No tenant data access, production actions, React, Vite, or xyflow are included.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `NR3_ADMIN_PASSWORD` and `NR3_SESSION_SECRET` in `.env`.

Onboarding leads are stored in SQLite at `data/nr3.db` by default. The database file is local runtime state and is not committed.

Email sending uses SMTP only when all SMTP environment variables are configured. Without SMTP, the admin screen generates a secure link and shows a manual-send preview without marking the email as sent.

The public onboarding link stores intake answers locally in SQLite. It is a controlled intake capture only; it does not create tenants, write Nr 2 configuration, or send data into production systems.

Admins can review submitted answers from `/admin/onboarding/leads/{lead_id}` and download a plain-text setup summary from `/admin/onboarding/leads/{lead_id}/setup-summary.txt`.

Review decisions are internal-only status markers. They do not create tenants, edit Nr 2, or write production configuration.

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8010
```

Open:

- `http://127.0.0.1:8010/healthz`
- `http://127.0.0.1:8010/admin`

## Test

```bash
pytest
```

## Docker

```bash
docker compose build
docker compose up
```

The service is named `wtyj-admin` and exposes port `8010`.

## Security Notes

- Admin access is protected by `NR3_ADMIN_PASSWORD`.
- The password is never placed in frontend code.
- Session cookies are signed with `NR3_SESSION_SECRET`.
- The nginx template includes an IP allowlist perimeter.
- No API keys, provider tokens, tenant secrets, or production credentials are displayed.
