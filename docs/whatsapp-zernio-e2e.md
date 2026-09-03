# WhatsApp/Zernio Client Authorization Flow - E2E Checklist

## Scope

This checklist verifies the WhatsApp Business authorization flow owned by Nr3.
Nr3's tenant-scoped bridge can read and mutate explicitly supported Nr2
controls, but this checklist does not deploy or authorize a separate Nr1/Nr2
service release.

## Per-release rollback point

Before every production authorization-flow release, record the exact reviewed
commit/image and create a fresh protected snapshot of the complete Nr3 data
tree, worker queue/lifecycle state, relevant tenant runtimes, and effective
service configuration. Follow the drain, quiesce, coordinated rollout, and
rollback sequence in [auto-provisioning.md](auto-provisioning.md); never reuse
an old snapshot name or deploy the app and queue worker independently.

## Required Environment

Set these on Nr3 before live testing:

```env
ZERNIO_API_KEY=...
ZERNIO_API_BASE_URL=https://zernio.com/api/v1
UNBOKS_PUBLIC_URL=https://unboks.org
UNBOKS_ADMIN_API_URL=https://icp.unboks.org/internal/api
```

Do not put the Zernio API key in templates, JavaScript, tenant JSON, or logs.

## Automated Test

Run from the repo root:

```bash
. .venv/bin/activate
python -m pytest -q
```

The mocked E2E test covers:

- operator login
- generating a WhatsApp authorization link
- receiving a callback
- marking the connection connected
- reading the status endpoint
- rendering the tenant workspace without exposing `ZERNIO_API_KEY`
- writing safe audit events

## Manual Live Checklist

1. Open `https://icp.unboks.org/admin`.
2. Log in as the internal admin.
3. Open the target tenant workspace.
4. Open `Channels`.
5. In `WhatsApp Business`, click `Generate authorization link`.
6. Copy the generated link.
7. Send the link to the client manually in the separate WhatsApp connection email.
8. Client opens the link in their own browser and approves Meta/Zernio access.
   The callback does not trust its query-string account ID: Nr3 fetches that
   account from Zernio and requires an active WhatsApp account whose profile ID
   exactly matches the tenant's connection request.
9. Client should land on one of:
   - `/connect/whatsapp/result?status=success`
   - `/connect/whatsapp/result?status=pending-number`
   - `/connect/whatsapp/result?status=pending-activation`
   - `/connect/whatsapp/result?status=failed`
10. In Nr3, click `Refresh status`.
11. If multiple phone numbers appear, select the correct client phone number.
12. If strict-allowlist repair is queued, the card remains pending and inbound
    routing stays blocked. Refresh only after the host worker has completed.
13. Confirm the card shows `Connected` and the expected phone number only after
    the strict runtime allowlist contains the same Zernio account ID.
14. Open `/admin/settings` and confirm audit entries were recorded.
15. Confirm the initial tenant welcome email did not include the WhatsApp authorization link.

## Expected API Behavior

- `POST /internal/api/tenants/{tenant}/channels/whatsapp/connect/start`
  returns a client authorization URL.
- `GET /internal/api/connect/whatsapp/callback`
  validates callback state, fetches the claimed account from Zernio, checks
  platform/activity/profile ownership, then updates stored connection state.
- `GET /internal/api/tenants/{tenant}/channels/whatsapp/status`
  returns one safe status object for the UI.
- `GET /internal/api/tenants/{tenant}/channels/whatsapp/phone-numbers`
  returns safe phone options only.
- `POST /internal/api/tenants/{tenant}/channels/whatsapp/phone-numbers/select`
  confirms the selected phone number.

## Security Checks

- Zernio API calls are backend-only.
- Raw callback state is never stored; only its hash is stored.
- Callback state is claimed atomically. Duplicate or concurrent deliveries are
  read-only and cannot downgrade a completed connection or switch accounts.
- A valid callback state alone cannot authorize an arbitrary account ID or an
  account owned by another Zernio profile.
- A tenant connection stays pending (or failed) until strict allowlist
  persistence succeeds or its exact repair job is safely queued.
- Unreadable or malformed tenant configuration is never rebuilt from an empty
  object; the host repair path fails closed instead.
- Callback audit events do not persist OAuth-like `code` values.
- API keys and provider credentials are not returned by JSON endpoints.
- Public result pages do not echo arbitrary query text.

## Known Boundary

This branch builds the authorization and connection-state foundation. It does
not send the WhatsApp connection email automatically and does not change Nr2 or
Nr1.
