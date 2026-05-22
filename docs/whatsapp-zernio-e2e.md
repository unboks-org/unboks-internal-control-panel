# WhatsApp/Zernio Client Authorization Flow - E2E Checklist

## Scope

This checklist verifies the Nr3-only WhatsApp Business authorization flow.
Nr1 and Nr2 are not part of this feature branch.

## Rollback Point

- VPS rollback directory: `/root/_nr3_rollbacks/whatsapp-zernio-auth-20260522-193520`
- Git rollback tag: `rollback-before-whatsapp-zernio-auth-20260522-193520`
- Feature branch: `feature/whatsapp-zernio-client-auth`

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
9. Client should land on one of:
   - `/connect/whatsapp/result?status=success`
   - `/connect/whatsapp/result?status=pending-number`
   - `/connect/whatsapp/result?status=failed`
10. In Nr3, click `Refresh status`.
11. If multiple phone numbers appear, select the correct client phone number.
12. Confirm the card shows `Connected` and the expected phone number.
13. Open `/admin/settings` and confirm audit entries were recorded.
14. Confirm the initial tenant welcome email did not include the WhatsApp authorization link.

## Expected API Behavior

- `POST /internal/api/tenants/{tenant}/channels/whatsapp/connect/start`
  returns a client authorization URL.
- `GET /internal/api/connect/whatsapp/callback`
  validates callback state and updates stored connection state.
- `GET /internal/api/tenants/{tenant}/channels/whatsapp/status`
  returns one safe status object for the UI.
- `GET /internal/api/tenants/{tenant}/channels/whatsapp/phone-numbers`
  returns safe phone options only.
- `POST /internal/api/tenants/{tenant}/channels/whatsapp/phone-numbers/select`
  confirms the selected phone number.

## Security Checks

- Zernio API calls are backend-only.
- Raw callback state is never stored; only its hash is stored.
- Callback audit events do not persist OAuth-like `code` values.
- API keys and provider credentials are not returned by JSON endpoints.
- Public result pages do not echo arbitrary query text.

## Known Boundary

This branch builds the authorization and connection-state foundation. It does
not send the WhatsApp connection email automatically and does not change Nr2 or
Nr1.
