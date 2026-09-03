# unboks-internal-control-panel

Private, owner-operated Unboks control panel and onboarding app (Nr 3).

The application includes:

- FastAPI backend
- Vanilla HTML/CSS/JS frontend
- `/healthz`
- password-protected `/admin`
- onboarding lead creation, review, and status tracking
- secure onboarding link generation
- welcome email sending or manual-send preview when SMTP is not configured
- token-gated one-question-at-a-time onboarding intake
- protected admin review of intake answers and text setup summary export
- internal review decision states for approved / needs changes
- tenant discovery and control-plane configuration
- verified Zernio/WhatsApp connection management
- backup import/export with target identity and credential isolation
- queue-v2 tenant provisioning, lifecycle actions, and durable deletion
- Docker service shape for `wtyj-admin` on port `8010`
- nginx IP allowlist template

Privileged Docker, nginx, filesystem, and systemd operations are never executed
by the web process. In production, the app publishes private jobs to a shared
durable queue and a root-owned host worker performs the reviewed operation.
See [docs/auto-provisioning.md](docs/auto-provisioning.md) for the protocol,
required paths, coordinated rollout, and recovery runbook.

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

The public onboarding link stores intake answers locally in SQLite. Public
signup does not provision by default. When email verification and the explicit
`NR3_PUBLIC_SIGNUP_AUTO_PROVISION_AFTER_VERIFY=true` production flag are both
present, it may reserve a tenant generation and publish a queue-v2 provision
job; the host worker still owns every privileged side effect.

Admins can review submitted answers from `/admin/onboarding/leads/{lead_id}` and download a plain-text setup summary from `/admin/onboarding/leads/{lead_id}/setup-summary.txt`.

Review decisions themselves are internal-only status markers.

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

## Production Operations

Production installation, deployment, rollback, queue recovery, and destructive
tenant operations are owner-only procedures for Calvin or an explicitly
delegated root operator. Running this repository, editing a unit file, or
publishing a job is not proof that production changed. The owner must execute
the commands in the deployment runbook and verify the exact service revision,
queue state, worker logs, health checks, and lifecycle ledger before declaring
success.

Do not run `host/nr3_provision_worker.py --once` alongside the systemd service.
The worker holds a nonblocking singleton lock in the queue directory; stop and
verify the service first if owner-directed manual recovery requires `--once`.

## Security Notes

- Admin access is protected by `NR3_ADMIN_PASSWORD`.
- The password is never placed in frontend code.
- Session cookies are signed with `NR3_SESSION_SECRET`.
- The nginx template includes an IP allowlist perimeter.
- No API keys, provider tokens, tenant secrets, or production credentials are displayed.
- Per-tenant bridge tokens are generated and rotated by the host worker; the
  legacy shared bridge-token fallback remains disabled by default.
- Tenant delete archives contain only that tenant's runtime. Exports serialize
  only explicitly tenant-scoped records and never copy the global Nr 3 data
  tree wholesale; that tree requires a separate owner-managed DR backup.
