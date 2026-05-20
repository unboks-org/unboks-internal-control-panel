# Current Tenant Creation Flow

Status: working v0.1 thin-control flow.

1. Internal admin opens `/admin/tenants/new`.
2. The form posts to `/admin/tenants/create`.
3. Nr 3 writes a flat `client.json` for the tenant and registers the tenant for the sidebar.
4. The host provisioner can create the tenant folder, `platform.env`, `docker-compose.yml`, nginx route, and container.
5. SMTP welcome email is sent only when configured and requested.
6. Channels and selected AI toggles are pushed through the Nr 3 internal override bridge.

Protected working paths:
- tenant sidebar discovery
- tenant creation
- SMTP welcome email
- channel state bridge
- tenant notes
- AI reply/auto-reply/learning toggles
- inactive/suspend flow
