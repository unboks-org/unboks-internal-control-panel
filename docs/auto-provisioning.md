# Nr 3 Automatic Tenant Provisioning

Nr 3 does not run Docker/nginx/systemctl directly from the web request.
The FastAPI app writes a provisioning job into the shared `data/`
volume, then a root host-side systemd worker consumes it.

Production commands in this document are an owner-only runbook for Calvin or
an explicitly delegated root operator. Neither the web app nor repository
automation installs, deploys, or validates the VPS service. A production
change is complete only after that operator runs the commands and verifies the
checked-out revision, unit, queue, lifecycle ledger, logs, and health checks.

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
   - a 2xx response from `http://127.0.0.1:{port}/health`
6. The worker writes the result to `data/provisioning/results`.
7. Nr 3 accepts the result only when its job, tenant, creation/generation, and
   payload ownership fields match the active operation.
8. The success page shows confirmed automatic success, a still-owned queued
   state, or a manual fallback only when automatic provisioning is disabled.
   A failed worker result never exposes stale credentials or deploy scripts,
   and releases its reservation only when the worker explicitly proves
   `safe_to_release=true`; failure defaults to unsafe.

## Queue Protocol v2

Provision and tenant-action jobs are private (`0600`) JSON files. The payload
`job_id` must exactly equal the queue filename stem. The worker accepts only an
explicit `job_type`, and provision jobs additionally require an unguessable
`creation_id`. Results echo the job type, job ID, tenant slug, action,
creation/generation identity, and payload digest—including failed action
results—and are ignored unless every required ownership field matches.
Malformed, non-object, stale, or ambiguous results fail closed.

Only one lifecycle operation may own a tenant slug at a time. An exact replay
of the same action and security-relevant parameters is idempotent; a different
action or different parameters fail closed instead of borrowing the active job
ID. New tenant creation also keeps an ownership claim until a matching terminal
result is reconciled.

Existing-runtime mutations (`suspend`, `unpause`, `restart`, password reset,
safe tenant-detail update, allowlist repair, and same-target restore) carry the
exact SHA-256 generation fingerprint derived from the mounted target runtime.
Clone/new-target restore instead carries its reserved `creation_id` and host
port. A delayed action can therefore never act on a later tenant that reused
the same slug.

Safe tenant-detail updates keep the web container's tenant mount read-only and
delegate the narrow `client.json` patch to the host worker. The worker accepts
only the eight public business fields exposed by the form, including separate
public phone and WhatsApp values, revalidates the
generation and payload, and holds the shared `config/client.json.lock` across
read/modify/atomic-replace. The privileged worker opens the tenant-writable
`client.json` with `O_NOFOLLOW`, verifies the opened descriptor is a regular
file, and reads from that descriptor, so a tenant cannot substitute another
tenant's config through a symlink. Every other process that writes
`client.json` must use that exact lock path and protocol; deploys must not
combine a locking worker with an older unlocked runtime writer.

Delete is a two-job operation bound to one durable delete ledger entry and one
generation fingerprint. `prepare_delete_tenant` creates the authorization
backup without deleting the tenant. Provider cleanup may begin only after that
result is verified. `delete_tenant` must present the exact prepared path and
digest, takes a second current defensive backup, proves exact container
absence, and only then removes the tenant tree, nginx route, and bridge token.

The host worker, not the web payload, generates the final Docker Compose and
managed nginx text. Caller-supplied text must first match that canonical shape;
extra mounts, ports, directives, or privileged options are rejected.
New canonical `platform.env` and Compose artifacts both force
`TENANT_RUNTIME_CONTROLS_REQUIRED=true` and
`TENANT_ACCOUNT_ALLOWLIST_REQUIRED=true`, so a cold start or Nr3/config outage
cannot fall back to legacy-open runtime behavior. Exact historical Compose
files remain recognizable only as existing tenant artifacts; provisioning
rejects them, while a restore rewrites them to the strict canonical form.

The worker holds a nonblocking `flock` on
`data/provisioning/jobs/.nr3-provision-worker.lock`. It scans orphaned
`*.processing` jobs before new `*.json` jobs. A claimed job is discarded only
when an exact correlated terminal result already exists; otherwise it is
reprocessed idempotently. Never delete or unlink the lock file to bypass a live
owner, and never run `--once` while the systemd worker is active.

## Durable State and Paths

All relative app paths below resolve inside the repository's durable `data`
volume. The host unit must point at the same physical directories.

| Path | Owner and purpose |
| --- | --- |
| `data/provisioning/jobs` | App publishes `*.json`; worker atomically claims `*.processing` and holds its singleton lock here. |
| `data/provisioning/results` | Worker publishes private, terminal correlated results; the app reads but does not trust them by filename alone. |
| `data/provisioning/failed` | Worker quarantine/copy of failed claimed jobs for owner diagnosis. |
| `data/provisioning/reconciled` | App markers for terminal results already reconciled after an HTTP timeout. |
| `data/provisioning/locks` | Per-slug queue publication locks. |
| `data/provisioning/create-locks` | Cross-process tenant lifecycle locks. |
| `data/provisioning/tenant_claims.json` | Creation/clone ownership claims and their job correlation. |
| `data/provisioning/delete-operations` | Delete ledger, per-slug locks, active generation bindings, completed history, and provider-orphan ledger. |
| `data/port_registry.json` | Durable slug-to-host-port reservations; release remains generation/claim gated. |
| `data/tenant_registry.json` | Durable control-plane tenant registry; writes are private and atomic. |
| `data/nr3.db` | Global SQLite state for onboarding, channel connections, password recovery, and related control-plane records; snapshot with SQLite-aware tooling or while both writers are stopped. |
| `data/public_signup_requests.json` | Public-signup verification, provisioning correlation, and credential-delivery outbox state. |
| `data/channel_state.json`, `data/icp_overrides.json`, `data/tenant_notes.json`, `data/prompt_conflict_resolutions.json`, and `data/nr2_knowledge_cache.json` | Shared control-plane stores whose entries are tenant-scoped and generation-cleaned; keep them with the global control-plane backup, never inside one tenant export. |
| `data/tenant_import_payloads` | Private uploaded packages approved for host restore; the worker rejects paths outside this root. |
| `data/tenant_exports` and `data/tenant_import_rollbacks` | Sensitive app-side export/rollback artifacts. |
| `/root/_deleted_tenants` | Root-only verified prepared/defensive tenant-runtime backups and crash-recovery manifests. |
| `/root/clients` | Live tenant runtimes plus hidden durable restore transactions. |

Keep the queue, claims, locks, generation bindings, and delete ledger together
during backup/restore. Copying only jobs or only results destroys correlation
and must not be used as recovery.

## Required VPS Setup

First configure the app-side paths in
`/root/unboks-internal-control-panel/.env`. Production restore must stay in
host-worker mode, and the tenant root must remain read-only in the web
container:

```bash
NR3_AUTO_PROVISION=true
NR3_PROVISION_QUEUE_DIR=data/provisioning/jobs
NR3_PROVISION_RESULT_DIR=data/provisioning/results
NR3_PROVISION_RECONCILED_DIR=data/provisioning/reconciled
NR3_PROVISION_CLAIMS_PATH=data/provisioning/tenant_claims.json
NR3_TENANT_CREATE_LOCK_DIR=data/provisioning/create-locks
NR3_DELETE_OPERATIONS_DIR=data/provisioning/delete-operations
NR3_PROVIDER_ORPHAN_LEDGER_PATH=
NR3_PROVISION_TIMEOUT_SECONDS=90
NR3_DB_PATH=data/nr3.db
NR3_PORT_REGISTRY_PATH=data/port_registry.json
NR3_TENANT_REGISTRY_PATH=data/tenant_registry.json
NR3_CHANNEL_STATE_PATH=data/channel_state.json
NR3_ICP_STATE_PATH=data/icp_overrides.json
NR3_TENANT_NOTES_PATH=data/tenant_notes.json
NR3_PROMPT_CONFLICT_RESOLUTIONS_PATH=data/prompt_conflict_resolutions.json
NR3_NR2_KNOWLEDGE_CACHE_PATH=data/nr2_knowledge_cache.json
NR3_PUBLIC_SIGNUP_REQUESTS_PATH=data/public_signup_requests.json
NR3_TENANT_IMPORT_PAYLOAD_DIR=data/tenant_import_payloads
NR3_TENANT_EXPORTS_DIR=data/tenant_exports
NR3_TENANT_IMPORT_ROLLBACK_DIR=data/tenant_import_rollbacks
NR3_TENANT_RUNTIME_RESTORE_MODE=host
NR3_TENANTS_CLIENT_DIR=/app/tenant_root
NR3_TENANTS_CLIENT_HOST_DIR=/root/clients
NR3_TENANT_BRIDGE_TOKEN_DIR=/app/tenant_root/_shared/nr3_bridge_tokens
NR3_ALLOW_LEGACY_SHARED_BRIDGE_TOKEN=false
```

The owner must deploy the same reviewed commit to the host checkout and web
image. Then, as root, install the reviewed unit and inspect the effective
configuration before enabling it:

```bash
install -o root -g root -m 0644 /root/unboks-internal-control-panel/host/nr3-provision-worker.service /etc/systemd/system/nr3-provision-worker.service
systemd-analyze verify /etc/systemd/system/nr3-provision-worker.service
systemctl daemon-reload
systemctl enable --now nr3-provision-worker.service
systemctl cat nr3-provision-worker.service
systemctl status nr3-provision-worker.service --no-pager
```

The worker expects:

- root access to Docker, nginx validation/reload, `/root/clients`, the shared
  queue, and the root-only backup directory;
- Docker and nginx installed, and
  `/etc/nginx/sites-enabled/api-unboks` resolving to a regular managed file;
- `/root/clients/_shared/nr3_bridge_tokens` on the same read-only tenant-root
  mount seen by the app. The worker creates and rotates one private file per
  tenant; a shared bridge-token file is not required;
- `/root/_deleted_tenants` on durable owner-only storage and
  `/root/nginx-sites-enabled-backups` outside `sites-enabled`;
- `NR3_ICP_DATA_DIR` in the unit pointing at the same host `data` directory
  that contains `tenant_import_payloads`;
- optional root-only `anthropic_api_key`, `late_api_key`, and
  `zernio_webhook_secret` files at the paths declared in the unit. Missing
  optional files are omitted from newly generated tenant environments; and
- the reviewed `wtyj-agent` Docker image available locally.

The checked-in unit sets `UMask=0077`, every host path explicitly, and uses the
queue singleton lock. Treat changes to those paths as a coordinated protocol
change, not a standalone service tweak.

## Required Coordinated Upgrade

Queue protocol v2 is intentionally incompatible with legacy provision jobs and
results. Never deploy only the web container or only the worker while jobs are
active. Use this sequence for the first v2 rollout:

1. Pause owner/public actions that can create provisioning, restore, lifecycle,
   or delete jobs. Disable verified-signup auto-provisioning for the window.
2. With the currently compatible app/worker pair still running, inspect
   `data/provisioning/jobs` for both `*.json` and `*.processing` files and let
   valid work reach correlated terminal results.
3. If the queue cannot drain, stop. Do not rename/delete a claimed job or feed a
   legacy job to the v2 worker. The owner must preserve the full state and
   complete a job-specific recovery first.
4. Confirm there are no `*.json` or `*.processing` files, then stop both the
   worker and `wtyj-admin`. Verify no second/manual worker holds the singleton
   lock.
5. While both writers are stopped, take a recoverable snapshot of the complete
   `data` tree, `/root/_deleted_tenants`, and any pending hidden restore state
   under `/root/clients`. This is also the safe point for the separate global
   ICP backup.
6. Deploy one reviewed source revision to the host checkout and build/recreate
   `wtyj-admin` from that exact revision. Install the unit from the same tree.
7. While tenant runtimes are still quiesced, inventory every existing
   `platform.env` and Compose file. Preserve each exact slug, host port, bridge
   token, and secret while setting both required-runtime flags to `true` and
   replacing an exact legacy Compose artifact with the strict canonical form.
   Reject any noncanonical Compose instead of normalizing it. Validate every
   migrated artifact with the reviewed worker before restarting containers.
8. Start the worker, then recreate/start the web container. Do not resume job
   creation between those two steps.
9. Verify the commit/image identity, effective unit, singleton ownership,
   worker logs, `/healthz`, an authenticated read-only tenant view, lifecycle
   claims/generations/delete ledgers, and an empty queue.
10. On the first rollout that adds provider-ownership verification, preflight
   every existing connected WhatsApp tenant through the authenticated status
   endpoint before external traffic resumes. The status reconciliation may
   mark a legacy row verified only when Zernio returns the exact stored account
   ID as an active WhatsApp account on that tenant's exact stored profile. Wait
   for any strict-allowlist host repair, then require `connected_healthy` and
   an empty queue for every existing sender. A missing, different, inactive, or
   ambiguously owned provider account is a stop condition and requires an
   explicit reconnect; never substitute another account from the same profile.
11. Re-enable owner actions and verified-signup provisioning only after all
    checks pass.

Rollback uses the same drain-and-quiesce rule. Restore the app, worker, and all
correlated durable state to one compatible snapshot/revision; never roll back
only one side of the queue protocol.

## Crash and Queue Recovery

- Restart the same compatible worker first. It scans `*.processing` before new
  jobs and safely resumes or retires them using exact terminal-result
  correlation.
- A singleton-lock error means another process owns the queue. Locate and stop
  the duplicate process; unlinking the lock pathname does not release its
  `flock` and can create split ownership.
- Keep `safe_to_release=false` failures, tenant claims, port reservations,
  generation bindings, delete ledgers, and hidden recovery manifests intact.
  They are deliberate quarantine, not stale clutter.
- Never manually mark a delete complete. A successful final result must bind to
  the ledger's operation/generation/prepared proof, include a verified current
  defensive backup, and prove runtime absence before Nr 3 forgets local state.
- Hidden `.nr3-delete-*` state in `/root/_deleted_tenants` and
  `.nr3-restore-*` state under `/root/clients` allow retries to restore or
  finish the exact owned generation. Remove them only through a reviewed,
  job-specific owner recovery after preserving a forensic copy.
- Preserve unreadable jobs/results in `failed` or an owner-controlled forensic
  snapshot. Do not edit a payload in place and retry it under the same job ID.
- If `provider-orphan-profiles.json` contains an entry, keep the affected
  tenant/channel disabled and verify ownership in Zernio. The supported tenant
  delete retry includes recorded orphan IDs and clears only those whose remote
  deletion succeeds. For an active tenant there is not yet a standalone ledger
  reconciliation command: delete only the exact recorded provider profile
  through an authorized provider workflow, retain the ledger, and use a
  reviewed recovery change to reconcile it. Do not edit the ledger manually,
  infer ownership from a profile name, or delete an ID assigned to another
  tenant.

An unavoidable provider boundary remains: a hard process or host failure at
any point after Zernio accepts profile creation but before Nr3 durably commits
either the local binding or orphan-ledger record can leave a profile whose ID
is unknown locally. After any interrupted create, an authorized operator must
inspect Zernio for an unassigned profile created at the matching time before
retrying. Keep the channel off until that sweep is complete. A future provider
API that supports idempotency keys or correlation-based profile listing should
replace this manual recovery step.

## Backup Boundaries and Global Disaster Recovery

Delete preparation stops/proves the exact container state, snapshots every
tenant-runtime SQLite database through SQLite's backup API, runs
`PRAGMA quick_check`, validates critical JSON, inventories/hashes the tree,
fsyncs files and directories, and restores/proves the prior running state.
Final delete revalidates that prepared authorization proof, quiesces again, and
publishes a fresh defensive snapshot before teardown. In the final result,
`prepared_backup_*` is the authorization proof and `backup_*` is the current
defensive recovery artifact.

Per-tenant export/delete packages never copy `NR3_ICP_DATA_DIR` wholesale.
Normal exports may serialize explicitly tenant-scoped records, while delete
snapshots contain the tenant runtime only. The global directory includes other
tenants' registry, connection, notes, audit, queue, generation, and deletion
state and must never be copied into one tenant's artifact. The repository does
not claim that tenant backup is global control-plane disaster recovery.

The owner must operate a separate encrypted, access-controlled global backup
for the complete Nr 3 `data` volume. Use an application-aware SQLite snapshot
or stop both app and worker before a filesystem snapshot, capture all queue and
lifecycle paths together, retain it outside per-tenant storage, and regularly
test a full restore into an isolated environment. Do not raw-copy a live SQLite
database and call it recoverable.

## Safety

- Slugs are validated before any host write.
- Existing tenant directories are not overwritten.
- New tenants start with AI replies, WhatsApp inbox, and Facebook DMs disabled
  and with a strict, empty provider-account allowlist.
- The host worker rotates a new per-tenant bridge token on every new provision
  and injects the target token on restore. Donor tokens are stripped, and a
  token is removed only after exact runtime teardown is proven.
- Worker results and asynchronous signup credential delivery are correlated to
  the current tenant creation claim before state is changed or email is sent.
- The durable delete ledger binds preparation, provider cleanup, final host
  teardown, and asynchronous reconciliation to one generation. Provider
  cleanup never starts before the prepared backup succeeds; local state is not
  forgotten without exact mounted-runtime absence and no new creation claim.
- Delete preparation and finalization quiesce the exact tenant container and
  create power-durable verified tenant-runtime snapshots (including SQLite
  `quick_check`).
  Per-tenant archives intentionally exclude the global Nr3 control-panel data
  directory because it contains other customers; that data must be protected
  by the separate control-panel disaster-recovery backup.
- Uploaded/app-writable Docker Compose is never executed by host restore.
  Existing targets retain their trusted canonical compose; clones receive a
  newly generated canonical compose from the reserved target slug and port.
- Uploaded provider verification, IDs, allowlists, and donor bridge credentials
  are untrusted. Same-target restore rebuilds only from locally captured trusted
  target state; clone/cross-tenant restore clears it.
- nginx config is backed up outside `sites-enabled` before insertion and
  atomically replaced, then restored if `nginx -t` fails. Route removal retries
  validate and reload nginx even when the marker is already absent on disk.
- Manual one-paste setup remains available only when automatic provisioning is
  disabled; it is not offered after a failed or still-running worker job.
