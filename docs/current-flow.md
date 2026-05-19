# Current Tenant Creation Flow (as of 2026-05-18)

## Problem Summary
- Creating a tenant in Nr 3 (ICP) often results in "Internal Server Error"
- `client.json` is not reliably written on the VPS
- Welcome email link leads to "Load Failed" or "workspace not recognized" in Nr 2
- Many legacy files and broken imports still exist

## Current Broken Flow
1. User clicks "+ Add New Tenant" in ICP
2. Form submitted to `/admin/tenants/create`
3. Code tries to create `client.json` (fails in many cases)
4. Welcome email is sent with link to Nr 2
5. Nr 2 fails to load tenant correctly

## Known Issues
- Broken imports in `tenant_io.py`
- Fragile virtual environment in Replit
- No reliable bridge between Nr 3 and VPS
- Old code and dead routes still present

---

Status: **Broken**
Goal: Make tenant creation reliable before any UI work.
