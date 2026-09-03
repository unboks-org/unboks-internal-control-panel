# TRACY control bridge timeout — September 3, 2026

## Confirmed cause

Production incoming Zernio messages at 23:27:55, 23:28:09 and 23:29:53 UTC reached Mermaid. Each control-bridge read timed out after three seconds; TRACY returned HTTP 503. The control panel remained available to other tenants. Host load, memory and disk were healthy.

The router held the exclusive tenant lifecycle file lock while forwarding to TRACY. Before acknowledging WhatsApp intake, TRACY synchronously fetched its authenticated runtime controls from the control panel. That bridge endpoint needed the same exclusive lifecycle lock. The router waited for TRACY, TRACY waited for the bridge, and the bridge waited for the router's lock. A successful `/health` response or a restart could not prove this path worked.

The mutually blocking leases were introduced with lifecycle isolation in control-panel commit f35076e (September 3). Earlier today there were also distinct interruptions: explicit container SIGTERM/stops, disabled restart policy (subsequently restored), and a persisted `ai_auto_reply` pause. Those are separate from the three confirmed late-evening bridge timeouts. Logs do not identify who requested every stop. Do not attribute every interruption to this code defect.

## Repair

Use shared lifecycle leases for ownership reads, generation-bound webhook forwarding and authenticated override reads. All use the same lock file as exclusive lifecycle mutations, so deletion/recreation and token rotation still wait until in-flight reads finish. Bridge writes remain exclusive. Disallow read-to-write lock upgrades. Keep generation validation, provider ownership, strict account allowlists and bridge-token authentication unchanged.

Run forwarding in asyncio's executor, separate from Starlette's synchronous endpoint thread pool. Otherwise enough concurrent forwarding requests can occupy every bridge callback worker and reproduce a different circular wait through thread-pool exhaustion.

Rejected alternatives: increasing timeouts only lengthens the outage; restarting or caching controls masks it temporarily; removing the lifecycle lease would allow tenant replacement during forwarding; disabling runtime controls would bypass operator pause and tenant protections.

## Verification

A regression test reproduces the complete router → tenant callback → authenticated override-read relationship: before repair it returns 503; after repair it returns 200. Additional tests verify shared reads, cross-process writer exclusion, forbidden upgrades and existing stale-generation/token/allowlist controls. Production verification must exercise the live router and callback relationship, not only `/health`, and confirm an actual provider-delivered reply when available.

## Rollback

Keep the current control-panel image tagged before deployment and record the deployed commit. Revert only the control-panel release if validation fails; do not alter Mermaid data, account routing, model credentials or other tenant containers. The known older image has this deadlock, so rollback is containment, not a repair.

## Production result and prevention

PR #97 is merged as a538d80. The exact tested image `unboks-control:tracy-bb8f49f` is pinned in the production Compose override and contains the same application code as that merge. The previous image is retained as `unboks-control:before-tracy-deadlock`.

After deployment, a signed replay of an already completed inbound message returned 200 in 72 ms and was safely deduplicated. A fresh real incoming message at 23:41:35 UTC produced a Claude reply; Zernio confirmed sent at 23:41:46 and delivered at 23:41:49. Inbox and AI controls were active and the WhatsApp account connected. The replay initially used a `sha256=` prefix unsupported by the tenant verifier; correcting the test to Zernio's raw digest format resolved that test-only 403.

`host/tracy_watchdog.py` and its systemd timer run on the VPS every minute, independently of the desktop or this conversation. The probe exercises an authenticated HTTP control read while holding the forwarding lifecycle read lease, and checks Mermaid's runtime health. Two consecutive process failures trigger a start/restart of the existing Mermaid container, with a ten-minute recovery cooldown. It never replaces an image, sends a WhatsApp message, changes tenant controls or restarts peer containers. Callback failures and paused/disconnected controls become visible unhealthy/attention states rather than ineffective restart loops.

Operational state: `/var/lib/unboks-tracy-watchdog/status.json`. Logs: `journalctl -u unboks-tracy-watchdog.service`. Planned maintenance: create `/root/clients/mermaid/.maintenance` before stopping TRACY; remove it only when the intended release is safe to run. This protects deliberate maintenance from the recovery loop. The watchdog is supplementary; Docker's existing restart policy and Supervisor remain enabled.
