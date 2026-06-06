# Pre-Hardening Baseline

Created: 2026-06-06

Scope: German Diesel audit, hardening, and simplification program for Unboks.

## Rollback Git References

Rollback branch pushed in each active repo:

- `unboks-org/unboks-internal-control-panel`: `rollback/pre-german-diesel-audit` at `318ec11`
- `unboks-org/unboks-dashboard-api`: `rollback/pre-german-diesel-audit` at `b6f7098`
- `BensonOpas/wtyj-agent`: `rollback/pre-german-diesel-audit` at `27b058a`
- `unboks-org/unboks-public-website`: `rollback/pre-german-diesel-audit` at `5211a8c`

Rollback tag pushed in each active repo:

- `pre-german-diesel-audit-v1`

## Production Source Baseline

Production host: `bluemarlin-agent`

Canonical production source paths:

- ICP/Nr3: `/root/unboks-internal-control-panel`
- Dashboard/Nr2: `/root/unboks-dashboard-api`
- Runtime/WTYJ: `/root/wtyj-agent-source`

Production commits at snapshot:

- ICP/Nr3: `318ec1166673cb2a550e29bcacde081dc980dab4`
- Dashboard/Nr2: `b6f7098ea247dc809225f3a8f268d51f124f96e4`
- Runtime/WTYJ: `27b058a8b23bfc4127d2dab06049294729c931f5`

## Runtime Containers

Containers observed at snapshot:

- `unboks-internal-control-panel-wtyj-admin-1`
- `wtyj-staging`
- `wtyj-unboks`
- `wtyj-wibrandt`

All active runtime app ports were bound to localhost in the observed `docker ps` output.

## Database Backup

Backup root:

- `/root/_nr3_rollbacks/german-diesel-preaudit-20260606-142825`

Database backups created:

- `/root/unboks-internal-control-panel/data/nr3.db`
- `/root/unboks-internal-control-panel/data/icp.db`
- `/root/clients/unboks/data/state_registry.db`
- `/root/clients/unboks/data/state.db`
- `/root/clients/wibrandt/data/state_registry.db`
- `/root/clients/wibrandt/data/state.db`

Integrity verification:

- `nr3.db`: ok
- `unboks/state_registry.db`: ok
- `wibrandt/state_registry.db`: ok
- Empty placeholder DB files were copied as-is and recorded.

## Configuration Export

Sensitive configuration archive:

- `/root/_nr3_rollbacks/german-diesel-preaudit-20260606-142825/config/current-config-sensitive.tar.gz`

Contents include runtime/admin configuration and nginx configuration. This archive is root-only and must be treated as sensitive because it may contain secrets or credentials.

Config archive verification:

- Tar archive readable: yes

## Recovery Verification

Non-destructive recovery verification completed:

- Backup files created under root-only rollback directory.
- Non-empty SQLite backups passed `PRAGMA integrity_check` using Python SQLite.
- Config archive passed `tar -tzf` readability check.
- Production health smoke checks passed after backup.

No destructive restore was run against live tenants.

## Health Smoke Checks

Observed after backup:

- ICP/Nr3 local health: ok
- Unboks runtime local health: ok
- Wibrandt runtime local health: ok
- Dashboard public login page: served

## Local Workspace Notes

Local dashboard repo had one pre-existing dirty file not included in rollback branch/tag:

- `artifacts/unboks/src/components/inbox/EscalationReplyComposer.tsx`

This baseline report does not modify runtime behavior.

## Next Step

Begin forensic audit only after this rollback baseline is accepted.
