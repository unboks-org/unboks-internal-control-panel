# Nr 3 Automatic Tenant Provisioning

Nr 3 does not run Docker/nginx/systemctl directly from the web request.
The FastAPI app writes a provisioning job into the shared `data/`
volume, then a root host-side systemd worker consumes it.

## Flow

1. Calvin submits `/admin/tenants/create`.
2. FastAPI builds the same tenant artifacts as the one-paste fallback.
3. If `NR3_AUTO_PROVISION=true`, FastAPI writes a job to
   `data/provisioning/jobs`.
4. The VPS worker `nr3-provision-worker.service` writes:
   - `/root/clients/{slug}/config/client.json`
   - `/root/clients/{slug}/config/platform.env`
   - `/root/clients/{slug}/docker-compose.yml`
5. The worker runs:
   - `docker network inspect/create unboks-control`
   - `docker compose up -d`
   - nginx route insertion for `/api/{slug}/`
   - `nginx -t`
   - `systemctl reload nginx`
   - `http://127.0.0.1:{port}/health`
6. The worker writes the result to `data/provisioning/results`.
7. The success page shows either automatic success or a manual fallback.

## Required VPS Setup

Install the worker as root:

```bash
cp /root/unboks-internal-control-panel/host/nr3-provision-worker.service /etc/systemd/system/nr3-provision-worker.service
systemctl daemon-reload
systemctl enable --now nr3-provision-worker.service
systemctl status nr3-provision-worker.service --no-pager
```

Enable app-side job creation in `/root/unboks-internal-control-panel/.env`:

```bash
NR3_AUTO_PROVISION=true
NR3_PROVISION_QUEUE_DIR=data/provisioning/jobs
NR3_PROVISION_RESULT_DIR=data/provisioning/results
NR3_PROVISION_TIMEOUT_SECONDS=90
```

The worker expects:

- Docker installed on the host.
- nginx installed on the host.
- `/etc/nginx/sites-enabled/api-unboks` exists.
- `/root/nginx-sites-enabled-backups` can be created for nginx backups.
- `/root/clients/_shared/nr3_internal_api_token` exists and is non-empty.
- The `wtyj-agent` Docker image exists locally.

## Safety

- Slugs are validated before any host write.
- Existing tenant directories are not overwritten.
- The bridge token is read by the host worker and is not shown in the UI.
- nginx config is backed up outside `sites-enabled` before insertion and
  restored if `nginx -t` fails.
- Manual one-paste setup remains available as fallback.
