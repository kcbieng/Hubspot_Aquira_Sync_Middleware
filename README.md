# Aquira ↔ HubSpot Middleware

Production middleware that syncs RCS Aquira radio traffic into HubSpot CRM: accounts vs advertisers, contacts, booked/proposal deals, and monthly expected revenue. HubSpot is source of truth for identity fields and writes those back to Aquira.

The scheduler honors what-if mode. Live writes only happen when you turn what-if off or explicitly force a live run.

## What it does

- Polls Aquira on a timer (default 30 minutes) and upserts companies, contacts, deals, and `revenue_period` records in HubSpot
- Reads HubSpot identity fields (name, phone, email, address, website) and sparse-writes them back to Aquira clients
- Optionally creates an Aquira client for a HubSpot company that has no `aquira_id`
- Allocates contract dollars across calendar months (spot-weighted when line data exists, even fallback otherwise)
- Maps Aquira sales reps to HubSpot owners
- Records every plan item and field diff so operators can inspect a what-if before enabling writes

## Portainer production stack

This repository is a one-stack Portainer deploy.

1. In Portainer: **Stacks → Add stack → Repository**.
2. Use `https://github.com/kcbieng/Hubspot_Aquira_Sync_Middleware` and the compose file `docker-compose.yml`.
3. Paste environment variables from [`stack.env.example`](stack.env.example) into the stack env editor. Set at least:
   - `AQUIRA_USERNAME` / `AQUIRA_PASSWORD`
   - `HUBSPOT_ACCESS_TOKEN` (private app with CRM scopes for companies, contacts, deals, owners, properties, and custom objects)
   - `POSTGRES_PASSWORD` (and the matching password inside `DATABASE_URL`)
   - `SETTINGS_FERNET_KEY` — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `UI_PASSWORD`
4. Leave `WHATIF=true` for the first deploy.
5. Deploy. The UI is on port **8080**.

Operator path after deploy:

1. Open `/ui/login` (default user `admin` unless you changed `UI_USERNAME`)
2. **Settings** — confirm URLs, save, **Test Aquira** and **Test HubSpot**
3. **Owners** — Suggest matches, review, save
4. Dashboard **Run what-if now** — inspect `/ui/runs/{id}` diffs
5. Type `WRITE` and **Force live sync** only after the plan looks right
6. Turn off what-if when you want the 30-minute scheduler to write

Postgres is not published to the host. Only the middleware port is.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
pytest -q
python -m app
```

- Health: `/health`
- Ready: `/ready`
- Login: `/ui/login`
- CLI: `python -m app sync --whatif`

Docker:

```bash
docker compose up --build
```

## API

```
GET  /health
GET  /ready
GET  /metrics
POST /sync/run { "whatif": true, "entities": ["companies","contacts","deals"], "aquira_id": null }
POST /sync/client/{aquira_id}
POST /sync/contract/{aquira_id}
GET  /sync/status
POST /webhooks/hubspot
```

The same routes also live under `/api/sync/*` for the operator UI.

## Source of truth

| Domain | Source | Notes |
|---|---|---|
| Aquira numeric IDs | Aquira | Stored on HubSpot as `aquira_id` |
| Contract / proposal / dollars / stations | Aquira | HubSpot is a projection |
| Company name, address, phone, website | HubSpot | Sparse writeback to Aquira Client |
| Contact name, email, phone | HubSpot | Writeback to Aquira |
| New HubSpot company with no `aquira_id` | HubSpot | Creates Aquira Client when enabled |
| Deal stage closed won | Derived | Booked Aquira contract |

Contracts are never written from HubSpot to Aquira.

## Tests

```bash
pytest -q
```
