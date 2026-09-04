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
- Add durable revenue-period persistence and stale cleanup for contract allocations
- Enforce the enabled owner-map rows during scheduling and deal assignment, instead of using loose suggestions
- Keep WHAT-IF planning and Aquira retry behavior in place while preserving the project’s SQLite-first local execution model
- Implement the HubSpot company and contact upsert flow, including parent-child company linkage and contact association

## Actual edits through this pass
- Added a durable RevenuePeriod table and repository helpers to store contract revenue periods and prune stale rows for a contract
- Enforced enabled owner-map lookups inside the sync orchestrator’s deal payload path so scheduled runs only use approved mappings
- Added a regression test covering revenue-period persistence and stale cleanup

## Risks and assumptions
- HubSpot property creation remains idempotent and relies on a pre-flight property listing
- Aquira response envelopes remain authoritative; every business failure is treated as an exception rather than a 200 OK
- The project’s runtime validation is blocked by a shell-level Python invocation issue in this environment, so test success is not yet confirmed here
- Contact/company association endpoints depend on the exact HubSpot object relationship type and property naming in the target portal

## Validation steps
- Run the project pytest task using the workspace venv after every edit
- Keep the integration layer incremental and avoid large rewrites until the sync contract is fully exercised by tests
- Re-check the HubSpot object association payloads after every new write path is introduced
- Confirm the terminal execution environment is healthy before claiming a green suite
