# Project map

## Target modules
- app/main.py — FastAPI bootstrap and health endpoints
- app/settings.py — environment-driven settings and runtime flags
- app/hubspot/client.py — HubSpot API access, owner lookup, and schema bootstrap
- app/integrations/aquira/service.py — Aquira session and request flows with 401 retry semantics
- app/aquira/contracts.py and app/mapping/revenue.py — contract normalization and revenue-period allocation
- app/db/models.py and app/db/repo.py — DB-backed sync state, run history, and mapping tables
- app/sync/orchestrator.py and app/sync/whatif.py — orchestration and safe planning flows

## Intended behavior
- Continue the live integration path without regressing the validated runtime foundation
- Bootstrap HubSpot custom properties as needed for Aquira client/contract synchronization
- Keep WHAT-IF planning and Aquira retry behavior in place before broader writes are enabled
- Preserve the project’s SQLite-first local execution model while preparing for production deployment
- Implement the HubSpot company and contact upsert flow, including parent-child company linkage and contact association

## Risks and assumptions
- Real HubSpot property creation must be idempotent and rely on a pre-flight property listing
- Aquira response envelopes remain authoritative; every business failure is treated as an exception rather than a 200 OK
- The current green pytest baseline is the safety gate before additional runtime features are expanded
- Contact/company association endpoints depend on the exact HubSpot object relationship type and property naming in the target portal

## Validation steps
- Run the project pytest task using the workspace venv after every edit
- Keep the integration layer incremental and avoid large rewrites until the sync contract is fully exercised by tests
- Re-check the HubSpot object association payloads after every new write path is introduced
