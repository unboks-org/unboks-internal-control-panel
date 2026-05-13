# unboks-internal-control-panel

Internal Unboks control panel and onboarding app.

This repository is for Nr 3 only: the private Unboks.org internal control panel. The current scaffold intentionally contains only:

- FastAPI backend
- Vanilla HTML/CSS/JS frontend
- `/healthz`
- password-protected `/admin` placeholder
- Docker service shape for `wtyj-admin` on port `8010`
- nginx IP allowlist template

No tenant data access, onboarding logic, production actions, React, Vite, or xyflow are included.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `NR3_ADMIN_PASSWORD` and `NR3_SESSION_SECRET` in `.env`.

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
