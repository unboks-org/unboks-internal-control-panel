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
