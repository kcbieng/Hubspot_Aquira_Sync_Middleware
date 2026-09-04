# Aquira ↔ HubSpot Middleware

A FastAPI-based integration service that synchronizes Aquira client, contact, and contract data into HubSpot, while preserving the local Aquira session model, FieldValue wrappers, safe what-if planning, and a minimal authenticated UI.

## Status

The project is implemented and verified against the current test suite.

## What this service does

- Polls Aquira for client/account and advertiser data on a timed schedule
- Maps Aquira client records into HubSpot company and contact objects
- Tracks contract/proposal deals and associated revenue periods
- Supports safe what-if planning without mutating external systems
- Exposes a full operator UI for login, dashboard, settings, owner mapping, runs, and event logs
- Uses SQLite by default for local development and supports environment-based DB configuration for deployment
- Includes webhook support, run history, owner mapping persistence, and background sync orchestration hooks

## Core architecture

- `app/main.py` — FastAPI app lifecycle, health endpoints, scheduler startup
- `app/api/routes.py` — API endpoints for login, settings, owner mapping, sync status
- `app/ui/routes.py` — login page and dashboard UI
- `app/webhooks/routes.py` — HubSpot webhook ingress
- `app/aquira/` — Aquira client/session logic, validation, and FieldValue unwrap helpers
- `app/hubspot/` — HubSpot property bootstrap, payload builders, and upserts
- `app/sync/` — orchestration, what-if planning, and transactional execution flow
- `app/db/` — database models, repositories, and cursor tracking
- `app/mapping/` — party classification and revenue allocation logic
- `tests/` — project test suite covering sync, HubSpot payloads, revenue logic, and smoke checks

## Project layout

```text
.
├── app/
│   ├── api/
│   ├── aquira/
│   ├── db/
│   ├── hubspot/
│   ├── integrations/
│   ├── jobs/
│   ├── mapping/
│   ├── services/
│   ├── sync/
│   ├── ui/
│   ├── webhooks/
│   ├── __init__.py
│   ├── __main__.py
│   ├── main.py
│   └── settings.py
├── tests/
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pytest.ini
├── LICENSE
├── Aquira_Swagger_docs.txt
├── CODEX_PROMPT_aquira_hubspot_middleware.md
├── PROJECT_MAP.md
└── README.md
```

## Quick start

### 1) Prerequisites

- Python 3.12
- A local or virtual environment
- Access to Aquira WebAPI credentials
- A HubSpot private app token if you want to hit live CRM APIs or test owner listing

### 2) Create the environment

```bash
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

On macOS/Linux:

```bash
source .venv/bin/activate
```

### 3) Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4) Configure runtime settings

For local development, copy the sample environment file:

```bash
copy .env.example .env
```

For Portainer or other stack-managed deployments, copy the stack template instead:

```bash
copy stack.env.example stack.env
```

Then edit the appropriate file with your values:

```env
AQUIRA_BASE_URL=https://aquira2go.kcbieng.org/Aquira_WebAPI
AQUIRA_USERNAME=
AQUIRA_PASSWORD=
HUBSPOT_ACCESS_TOKEN=
DATABASE_URL=sqlite:///./app.db
UI_USERNAME=admin
UI_PASSWORD=admin
WHATIF=true
SYNC_INTERVAL_MINUTES=30
SYNC_CALLS=false
SYNC_CREATE_AQUIRA_CLIENT=true
BOOTSTRAP_HUBSPOT=true
LOG_LEVEL=INFO
```

The default UI login is:

- Username: `admin`
- Password: `admin`

### 5) Run the app

```bash
python -m app
```

Or:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

The service will be available at:

- Health: `http://localhost:8080/health`
- Ready: `http://localhost:8080/ready`
- Login: `http://localhost:8080/ui/login`
- Dashboard: `http://localhost:8080/ui`
- Settings: `http://localhost:8080/ui/settings`
- Owner map: `http://localhost:8080/ui/owners`
- Runs: `http://localhost:8080/ui/runs`
- Event log: `http://localhost:8080/ui/logs`

### 6) Operate the app

After logging in with the default admin credentials:

- Use the dashboard to toggle what-if mode and trigger a sync
- Use settings to save interval and runtime values
- Use owner mapping to review Aquira vs HubSpot rep mapping
- Review the run history and item diffs under the runs pages
- Check debug output in the event log

## Docker quick start

Build and run with Docker Compose:

```bash
docker compose up --build
```

For Portainer stacks, the app service expects a generated `stack.env` file with the same variable names as [.env.example](.env.example) and [stack.env.example](stack.env.example).

The container exposes the middleware on port `8080` and reads configuration from the environment file provided by the deployment platform.

## Environment configuration

The application settings live in `app/settings.py` and are loaded from `.env` automatically. Important values include:

- `AQUIRA_BASE_URL` — Aquira API base URL
- `AQUIRA_USERNAME` / `AQUIRA_PASSWORD` — Aquira service credentials
- `HUBSPOT_ACCESS_TOKEN` — HubSpot Private App token
- `DATABASE_URL` — SQLite or Postgres DSN
- `WHATIF` — when true, validate and print planned writes without executing them
- `SYNC_INTERVAL_MINUTES` — poll interval for scheduled syncs
- `SYNC_CREATE_AQUIRA_CLIENT` — allow HubSpot-created clients to write back to Aquira
- `SYNC_CALLS` — enable optional call sync flow behind a feature flag
- `BOOTSTRAP_HUBSPOT` — enable property bootstrap at startup
- `UI_USERNAME` / `UI_PASSWORD` — UI authentication values

## Runtime behavior

### Safe sync model

The application runs in a guarded pattern:

1. Read from Aquira and HubSpot
2. Build a plan with exact matching and identity mapping
3. Execute in what-if or live mode based on configuration
4. Record sync state, cursors, and job history in the database

### What-if mode

When `WHATIF=true`, the app exercises live reads but avoids mutating external systems. This is the recommended mode for validation before live writes.

### Job scheduler

The FastAPI app starts a background APScheduler-based job when the service begins. The scheduler is configured in `app/main.py` and can be extended with additional jobs as needed.

## API overview

### Health and status

- `GET /health`
- `GET /ready`
- `GET /metrics`

### UI routes

- `GET /ui/login`
- `POST /ui/login`
- `GET /ui`
- `GET /ui/settings`
- `GET /ui/owners`
- `GET /ui/runs`
- `GET /ui/runs/{id}`
- `GET /ui/logs`

### API routes

- `POST /api/login`
- `POST /api/logout`
- `GET /api/settings`
- `PUT /api/settings`
- `POST /api/settings/test/aquira`
- `POST /api/settings/test/hubspot`
- `GET /api/owners/aquira`
- `GET /api/owners/hubspot`
- `GET /api/owners/map`
- `GET /api/owners/suggest`
- `PUT /api/owners/map`
- `GET /api/sync/status`
- `POST /api/sync/run`
- `GET /api/sync/runs`
- `GET /api/sync/runs/{id}`
- `PUT /api/settings/whatif`

### Webhook endpoints

- `POST /webhooks/hubspot` validates the HubSpot signature and deduplicates message IDs before any writeback work is queued.

## Database and sync state

The service tracks state in SQLite by default using SQLAlchemy models under `app/db/`. Core tables include sync cursors, id maps, run state, dead-letter events, and owner mapping records.

## Testing

Run the project test suite:

```bash
pytest -q
```

The current verified baseline is green with the project venv:

```text
48 passed in 5.48s
```

## Troubleshooting

### `python-multipart` missing

FastAPI form parsing requires the `python-multipart` package. If the login form fails with:

```text
The `python-multipart` library must be installed to use form parsing.
```

Install it into the active environment:

```bash
python -m pip install python-multipart
```

### Login route fails

Confirm the UI credentials in `.env` or the settings object match the values used for the POST request.

### No HubSpot writes happen

Check that `WHATIF` is false only when you intend live writes, and ensure `HUBSPOT_ACCESS_TOKEN` is set if live CRM calls are enabled.

### DB issues

If local tables look stale or migration errors appear, remove the local database file and rebuild as needed for development, or switch the `DATABASE_URL` to a clean SQLite or Postgres target.

## Security notes

- Secrets should be kept in `.env` or a secure environment manager; do not commit real credentials
- The app logs and masks secret-like values in API responses
- Webhook request validation should be enforced before any processing logic proceeds
- What-if mode is the default safe operational mode for validation and dry runs

## Recommended development flow

1. Create and activate a Python venv
2. Install dependencies from `requirements.txt`
3. Configure `.env` from `.env.example`
4. Run `pytest -q` to validate the baseline
5. Start the app with `python -m app`
6. Validate health and login endpoints before enabling live writes
7. Switch to `WHATIF=false` only after confirming the intended plan output

## License

This project is distributed under the MIT license. See `LICENSE` for details.

