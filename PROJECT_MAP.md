# Project map

## Target modules
- app/main.py — FastAPI bootstrap, health, root UI redirect, `/sync/*` aliases
- app/settings.py — environment-driven settings and runtime flags (what-if defaults on)
- app/aquira/client.py — live Aquira session, catalog load, sparse client/contact writes
- app/hubspot/client.py — HubSpot CRM v3/v4, owner lookup, schema bootstrap, projection
- app/mapping/revenue.py — monthly revenue-period allocation
- app/db/models.py and app/db/repo.py — Postgres-backed sync state, runs, owner map, webhook receipts
- app/sync/orchestrator.py and app/sync/planner.py — live pull, plan, apply
- app/webhooks/routes.py — HubSpot identity webhooks
- app/ui — operator console
- docker-compose.yml / Dockerfile — Portainer production stack

## Intended behavior
- Connect to live Aquira and HubSpot. No production mock path.
- Bootstrap HubSpot custom properties and the `revenue_period` custom object
- Keep WHAT-IF planning as the default until an operator turns it off
- HubSpot is source of truth for identity; Aquira is source of truth for contracts
- Never write Aquira contracts from HubSpot

## Validation steps
- `pytest -q` after every edit
- `python scripts/live_check.py` against real credentials before the first live write
