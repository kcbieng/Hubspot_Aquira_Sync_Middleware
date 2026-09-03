# Codex Prompt: Aquira ↔ HubSpot middleware

Build a fully functional, production-ready middleware service that synchronizes RCS Aquira Radio Traffic with HubSpot CRM.

Do not invent missing Aquira endpoints. Use only the Aquira Web API described below. Prefer working, tested code over commentary. If a mapping is ambiguous, implement it behind a config flag and document the default.

**Build order (mandatory):** lock the schemas/fixtures in this prompt → write the failing unit tests listed under Locked decisions → implement until those tests pass → then UI/Docker. Do not start the orchestrator or HubSpot writes until tests 1–4 are green.

---

## Goal

One Dockerized service that:

1. Polls Aquira every 30 minutes and upserts records into HubSpot.
2. Accepts HubSpot webhooks + periodic HubSpot pulls so client/contact identity changes flow back to Aquira.
3. Models radio sales correctly: Account (billing entity) vs Advertiser (on-air brand/program), multi-station contracts, proposals vs booked contracts, **monthly expected revenue** (not a single lump booked into the sign month).
4. Runs in `whatif` mode that exercises live reads against both systems and prints the planned writes without mutating Aquira or HubSpot.
5. Ships a basic authenticated Web UI (same container) to enter credentials, test connections, toggle what-if/interval, run a sync, and build the Aquira sales-rep ↔ HubSpot owner map from live lookups.

System of record:

| Domain | Source of truth | Notes |
|---|---|---|
| Aquira numeric IDs | Aquira | Stored on every HubSpot record as `aquira_id` |
| Contract / proposal / flight / dollars / stations | Aquira | HubSpot is a projection |
| Company legal/display name, address, phone, website | HubSpot | Write back to Aquira Client sparse fields |
| Contact name, email, phone | HubSpot | Write back to Aquira Client contacts |
| New company created in HubSpot with no `aquira_id` | HubSpot | Create Aquira Client, then stamp `aquira_id` |
| Deal pipeline stage "closed won" | Derived | Aquira booked contract (`IsContract` and not cancelled) → won. Proposal → open pipeline stage. |

Never write Aquira Contracts from HubSpot in v1. Contract edit locks and FieldValue ripples make that unsafe.

---

## Aquira API (authoritative)

Base URL (configurable, default this host):

`https://aquira2go.kcbieng.org/Aquira_WebAPI`

Swagger: `https://aquira2go.kcbieng.org/Aquira_WebAPI/swagger/docs/v1`

### Session / auth

- `POST /Session/Post` with username + password (plaintext at API layer; transport is TLS).
- Server sets ASP.NET session cookie and `Aquira_ID` cookie. Persist both on the HTTP client for the session lifetime.
- `HEAD /User/HeartBeat` to detect expiry. On 401, re-login once and retry the failed call.
- `DELETE /Session/Delete` on shutdown.
- Dedicated Aquira service user will be created later. Credentials from env **or** the Web UI settings store (UI wins if both set).
- Permissions follow the Aquira role of that user. Code must handle `Success=false` and `Error` codes; never assume 200 means business success.

### Response envelope (every call)

```json
{
  "Success": true,
  "Error": 0,
  "ErrorName": "string",
  "ErrorText": "string",
  "name": "string",
  "Entity": {},
  "Data": [],
  "Errors": "string"
}
```

`Error === -16` means validation failure; parse `Errors`.

### FieldValue wrapper

Most entity fields are:

```json
{ "Value": {}, "Valid": true, "Label": "string", "Access": 0, "Clear": true }
```

- Unwrap `.Value` when reading.
- On write, send **sparse** updates: only fields with write access.
- After any PUT/Update, replace local model with the returned Entity (ripple updates).
- Access enum: treat non-writable fields as read-only. Do not send them.

### Entity uniqueness

IDs are unique per entity type, not globally. Always store `(entity_type, aquira_id)` in the sync state DB.

Lookups that search by text are `contains`. After lookup, filter client-side for exact Name/ID match.

### Endpoints to use (v1)

Clients (Account + Advertiser live here):

- `POST /Client/Search`, `POST /Client/AdvancedSearch`, `POST /Client/SearchByID`
- `POST /Client/Lookup`, `POST /Client/LookupContacts`
- `POST /Client/Load/{id}`, `POST /Client/Create`
- `PUT /Client/Put` or `POST /Client/Update` with `{ Save: true, Sparse: true, Entity: {...} }`
- `POST /Client/Cancel` if an edit session was opened and must be abandoned
- `GET /Client/Get` if it returns a full list; otherwise page via Search

Contracts / proposals:

- `POST /Contract/Search`, `POST /Contract/AdvancedSearch`
- `POST /Contract/Load/{id}`
- `POST /Contract/Lookup`, `GET /Contract/Get`
- `POST /Contract/History`
- `POST /Contract/GetContractDetailAnalysis`, `POST /Contract/GetSpotLineDetailAnalysis`
- `POST /Contract/LoadSpotline`, `POST /Contract/LoadSpotlineStationSpots`
- Do **not** call Edit/Put/Delete/Create on contracts in v1.

Supporting lookups:

- `POST /Station/Lookup|Search|Load/{id}`
- `POST /StationGroup/Lookup|Load/{id}`
- `POST /User/Lookup` (`salesReps: true`)
- `POST /Product/Lookup`, `POST /TaxGroup/Lookup`
- `POST /RateCard/Lookup|Search`
- `POST /Call/SearchByClient` (phase 1.1)
- `POST /SpotMedia/Search` (optional, not required for v1 deals)
- `GET /AquiraAPI/Version`, `GET /AquiraAPI/ErrorCodes`, `GET /GlobalSettings/Get`

Invoices: there is no first-class Invoice resource in the documented surface. Implement `InvoiceProvider` as an interface with:

- `AquiraReportInvoiceProvider` stub that can later call `POST /Report/RunDirectReport` / `RunAdvancedSearchReport` if an invoice report is identified.
- `NullInvoiceProvider` default.
- Persist any invoice-like payload into HubSpot custom object `invoice` **or** deal properties so a future Stripe checkout manager can attach payment links. Do not block v1 on invoices.

If an endpoint payload shape is unclear, write a fixture from the swagger examples and an adapter that normalizes FieldValues.

---

## HubSpot

Use HubSpot CRM APIs v3 (or current stable object APIs).

Auth: Private App token in env (`HUBSPOT_ACCESS_TOKEN`).

Prefer standard objects and existing properties. Create `aquira_*` custom properties only when no standard property fits. On first boot, ensure properties exist via the Properties API (idempotent create).

### Object model

**Company** = commercial party.

Two company types, encoded as enumeration property `aquira_party_type`:

- `account` — Aquira Client where `IsAccount=true`. This is the billing entity / agency / broker / self-billing advertiser. **This is the primary HubSpot client.**
- `advertiser` — Aquira Client where `IsAdvertiser=true`. This is the on-air brand/program.

Rules:

- If one Aquira Client is both account and advertiser, create **one** Company with `aquira_party_type=both`.
- If Advertiser Client A has Account Client B (B ≠ A), create two Companies and associate Advertiser as child of Account using HubSpot parent-child company association (`parent company`).
- Deal associates to the **Account** company (billing client). Also associate the Advertiser company when distinct.
- Contacts associate to the Account company; if a contact only exists on the advertiser record, associate to both.

HubSpot parent/child companies are the agency split. Do not invent a custom Agency object unless parent association cannot be created on this portal; then fall back to a custom object `agency_link` and log a warning.

**Contact** = people on the Aquira Client (`LookupContacts` / contacts collection on Load).

Match order:

1. `aquira_id` + `aquira_entity_type=contact` (or composite `client_id:contact_key`)
2. email (lowercase)
3. name + phone

**Deal** = one Aquira Contract or Proposal.

- `IsProposal=true` and not booked → pipeline stage `proposal` (or existing equivalent; map in config).
- Booked contract (`IsContract=true`, not deleted/cancelled) → stage **closed won**.
- Cancelled / killed → closed lost (or a configured stage).
- `dealname`: `{ContractCD or Name} — {Advertiser}`.
- `closedate`: contract end date (flight end), not sign date.
- `amount`: total contract value from Aquira (informational).
- Custom properties:
  - `aquira_id`, `aquira_version`, `aquira_contract_cd`, `aquira_status`
  - `aquira_is_proposal`, `aquira_is_contract`, `aquira_sign_date`
  - `aquira_start_date`, `aquira_end_date`
  - `aquira_stations` (semicolon-separated names)
  - `aquira_account_id`, `aquira_advertiser_id`
  - `aquira_sales_rep`
- Owner: map Aquira SalesRep → HubSpot owner via the Web UI mapping table (`owner_map` in the DB). YAML import/export is optional. Unmapped reps: leave owner empty and log.

**Monthly expected income (required)**

Do **not** treat Deal `amount` + sign-month as forecast.

Create a HubSpot custom object `revenue_period` (labels: "Revenue Period" / "Revenue Periods"):

Properties:

- `aquira_id` (unique): `{contractId}:{yyyy-mm}:{stationId or 0}`
- `period` date (first of month)
- `amount` number
- `station` string
- `station_id` number
- `kind` enumeration: `proposal` | `booked`
- `contract_cd` string

Associations: Deal, Account Company, Advertiser Company (if distinct).

Derivation algorithm (implement exactly, unit-test it):

1. Load contract + spot lines / detail analysis.
2. For each booked (or proposed) dollar that has a date range and optional station:
   - Split the line amount across calendar months in `[start, end]` inclusive.
   - Default allocation: even split by number of months the line touches.
   - If daily/spot counts exist on the line, weight by spots or seconds in that month instead of even split.
3. Sum by `(contractId, year-month, stationId)`.
4. Upsert those periods. Delete HubSpot revenue_period records for that contract whose `aquira_id` is no longer produced (stale months after a contraction).

If spot-line dates cannot be obtained from Load/detail analysis, fall back to spreading `amount` evenly across months from contract start to end and set `kind` accordingly. Log `allocation=fallback`.

**Line items:** optional mirror of revenue_period onto Deal line items when Products are easy; revenue_period is mandatory.

**Calls (phase 1.1, behind flag `SYNC_CALLS=true`, default false):** `POST /Call/SearchByClient` → HubSpot engagements/calls associated to Company + Contact.

---

## Bidirectional client sync

### Aquira → HubSpot (every 30 min + manual)

For each Aquira Client in scope:

1. Load client + contacts.
2. Upsert Account and/or Advertiser companies.
3. Set parent association when account ≠ advertiser.
4. Upsert contacts and associations.
5. Record `aquira_version` and last-seen hash in local state.

### HubSpot → Aquira (webhooks + 30 min sweep)

HubSpot is SoT for:

- Company name, domain, phone, address fields
- Contact first/last, email, phone

Rules:

- Only write to Aquira Client if HubSpot record has `aquira_id`.
- If HubSpot company has no `aquira_id` and `sync_create_aquira_client=true` (default true) and party type is account/both:
  - `POST /Client/Create` then sparse Put with name/address/phones/emails.
  - Stamp returned ID onto HubSpot `aquira_id`.
- Advertiser-only companies without account: do not auto-create until an account parent exists (config flag to override).
- Before Put, `Load/{id}`, send sparse FieldValues only, `Save=true`.
- Conflict: if Aquira `Version` != last synced version AND HubSpot updated_at is newer on identity fields, still write HubSpot identity fields (SoT) but never touch non-identity Aquira fields.
- What-if mode: log the sparse payload, do not Put/Create.

Do not sync HubSpot deals back to Aquira.

---

## Sync state

Local Postgres (default) or SQLite for dev.

Tables:

- `sync_cursor(job, last_started, last_finished, last_error, last_success_at)`
- `id_map(entity_type, aquira_id, hubspot_object_type, hubspot_id, aquira_version, content_hash, updated_at)`
- `job_event(id, ts, job, level, message, payload_json)`
- `dead_letter(id, ts, entity_type, aquira_id, error, payload_json, attempts)`
- `app_settings(key, value_enc, updated_at)` — secrets encrypted at rest with `SETTINGS_FERNET_KEY`
- `owner_map(aquira_user_id, aquira_name, aquira_email, hubspot_owner_id, hubspot_name, hubspot_email, enabled, suggested, updated_at)`
- `ui_user` — single admin hash for the Web UI (or use `UI_USERNAME` / `UI_PASSWORD` from env on first boot)
- `sync_run(id, started_at, finished_at, trigger, whatif, status, summary_json, error)`
- `sync_run_item(id, run_id, entity_type, aquira_id, hubspot_id, action, diff_json, error)`

Idempotency: skip HubSpot PATCH when content_hash unchanged. Always safe to re-run.

---

## Runtime / packaging

- Python 3.12
- FastAPI app + APScheduler (or equivalent) for the 30-minute job
- `httpx` + cookie jar for Aquira
- Official HubSpot client or raw httpx
- SQLAlchemy 2.x
- Pydantic v2 settings
- Docker Compose + single Dockerfile
- Portainer-friendly: one stack file, named volumes for postgres + logs, env file
- Health: `GET /health` (process up), `GET /ready` (can login Aquira heartbeat OR report last success; HubSpot token present)
- Metrics: sync counts, errors, last duration on `GET /metrics` (JSON is fine)
- Timezone: America/Chicago

API / CLI:

```
POST /sync/run { "whatif": true, "since": null, "entities": ["clients","contracts"] }
POST /sync/client/{aquira_id}
POST /sync/contract/{aquira_id}
GET  /sync/status
POST /webhooks/hubspot   (signature validation)
```

Web UI (session cookie after login) under `/ui` and JSON under `/api/settings/*` — see Web UI section.

CLI: `python -m app sync --whatif --entities clients,contracts`

Config via env (bootstrap / override). Runtime values also live in `app_settings` after the UI saves:

```
AQUIRA_BASE_URL
AQUIRA_USERNAME
AQUIRA_PASSWORD
HUBSPOT_ACCESS_TOKEN
HUBSPOT_CLIENT_SECRET   # webhook signature
DATABASE_URL
SETTINGS_FERNET_KEY     # required in production; generate one and store in Portainer secrets
UI_USERNAME=admin
UI_PASSWORD             # initial admin password; force change on first login
SYNC_INTERVAL_MINUTES=30
WHATIF=false
SYNC_CALLS=false
SYNC_CREATE_AQUIRA_CLIENT=true
LOG_LEVEL=INFO
```

Never log passwords, cookies, or tokens. Never return secret values in full from the settings API — mask as `••••last4`.

---

## What-if / test mode

`WHATIF=true` or `--whatif`:

- Live **GET/Search/Load** against Aquira and HubSpot allowed.
- All Aquira Create/Put/Update/Delete skipped.
- All HubSpot create/update/associate/delete skipped.
- Emit a structured plan: entity, action (`create|update|skip|delete-stale`), keys, field-level diff (`from` → `to`). Persist that plan as a `sync_run` so the Web UI can render it.
- Exit 0 if planning succeeded; non-zero if a live read failed.
- Global what-if is a persisted setting (`app_settings.whatif`). The 30-minute scheduler **must honor it**. If what-if is on, scheduled jobs are plan-only. The UI can still force a one-shot live write by explicitly passing `whatif=false` on that run (confirm dialog required).

Unit tests use fixtures copied from swagger examples (FieldValue-wrapped Client + Contract). No network.

Add `scripts/smoke_whatif.py` that runs one client search + one contract search against live Aquira when credentials exist, then exits. Document it as optional.

---

## Web UI (required)

Same container, served by FastAPI. No separate frontend repo. Use server-rendered templates (Jinja2) plus a small amount of vanilla JS. Keep it ugly-functional: station-ops tool, not a marketing site.

Bind to `0.0.0.0:8080` (compose maps 8080). Protect `/ui` and `/api/settings` with a session cookie (httponly, samesite=lax). `/health`, `/ready`, `/metrics`, `/webhooks/hubspot` stay unauthenticated except webhook signature.

### Pages

1. **Login** `/ui/login`
2. **Dashboard** `/ui`
   - Connection chips (Aquira / HubSpot).
   - **What-if toggle** (on/off). Saving it updates `app_settings.whatif` immediately and is what the scheduler uses. Label must show current mode in plain language: `PLAN ONLY — no writes` vs `LIVE WRITES`.
   - Last scheduled run + last manual run: time, duration, whatif, counts (create/update/skip/delete-stale/error).
   - Actions (all from this page, no CLI required):
     - **Run what-if now** — force a full plan against live systems, `whatif=true`, ignore the toggle for this click only if toggle is already on (same result).
     - **Force live sync** — `whatif=false` for this run only. If the global toggle is ON, require typed confirm (`WRITE`).
     - **Force sync one client / one contract** — inputs for Aquira ID, plus what-if checkbox defaulting to the global toggle.
     - Cancel is not required if a job is running; disable buttons and show “running…” until `sync_run.finished_at` is set. One job at a time (lock).
   - After a run finishes, auto-navigate to `/ui/runs/{id}`.
3. **Connections** `/ui/settings`
   - Aquira: base URL, username, password
   - HubSpot: private app token, webhook client secret
   - Options: interval minutes, what-if default, sync calls, create Aquira client from new HubSpot company, bootstrap HubSpot schema
   - Buttons: Save, Test Aquira, Test HubSpot
   - Test Aquira: `POST /Session/Post` + `GET /AquiraAPI/Version` + `HEAD /User/HeartBeat`, then logout. Show WebApiVersion / AquiraVersion or the error envelope.
   - Test HubSpot: `GET /crm/v3/owners` (limit 1) + account info if available. Show portal id / token scopes error clearly.
4. **Owner map** `/ui/owners`
   - Left column: Aquira sales reps from `POST /User/Lookup` with `{ "salesReps": true, "CurrentOnly": true, "SearchTerm": "" }` (and copywriters toggle).
   - Right column: HubSpot owners from `GET /crm/v3/owners`.
   - Each Aquira row: dropdown of HubSpot owners + enabled checkbox.
   - **Suggest matches** button: auto-map by exact email, then case-insensitive name. Mark those rows `suggested=true` until the user saves.
   - Save writes `owner_map`. Unmapped + enabled=false means "do not assign owner".
   - Refresh pulls live lists again without wiping unsaved dropdowns (confirm if dirty).
5. **Runs / diffs** `/ui/runs` and `/ui/runs/{id}`
   - List last 50 runs: trigger (`schedule|manual|single`), whatif badge, status, counts, timestamp.
   - Detail page is the primary operator view:
     - Filter by entity type, action, text search on name/id.
     - Each row: entity, Aquira id, HubSpot id, action, collapsed field diff (`field: old → new`). Expand raw JSON.
     - What-if runs use the same table; banner: `No writes were sent`.
     - Live runs banner: `Writes were sent to HubSpot and/or Aquira`.
     - Download run as JSON.
6. **Event log** `/ui/logs` — last 200 `job_event` rows, filter by level. This is the debug stream; diffs live on Runs.

### Settings API

```
POST /api/login
POST /api/logout
GET  /api/settings            # masked secrets
PUT  /api/settings            # partial update
POST /api/settings/test/aquira
POST /api/settings/test/hubspot
GET  /api/owners/aquira       # live lookup
GET  /api/owners/hubspot      # live owners
GET  /api/owners/map
PUT  /api/owners/map          # [{aquira_user_id, hubspot_owner_id, enabled}]
POST /api/owners/suggest
GET  /api/sync/status
POST /api/sync/run              # { whatif, entities, aquira_client_id?, aquira_contract_id? }
GET  /api/sync/runs
GET  /api/sync/runs/{id}        # includes items + diffs
PUT  /api/settings/whatif       # { enabled: bool } — scheduler reads this
```

All of the above require the UI session except `/api/login`.

### Security rules

- Encrypt secret settings with Fernet (`SETTINGS_FERNET_KEY` from env). If the key is missing in dev, derive from a local file in the volume and print a one-time warning.
- Changing Aquira or HubSpot credentials drops the in-memory Aquira session and rebuilds clients.
- Do not persist HubSpot/Aquira passwords in browser storage.
- CSRF: same-origin form POST + session; JSON PUT sends the session cookie only.

### Owner mapping behavior in sync

`mapping/owners.py` reads **only** `owner_map` where `enabled=true` and `hubspot_owner_id` is set. File-based YAML is import-only (optional `POST /api/owners/import`).

---

## Architecture

```
app/
  main.py                 # FastAPI + scheduler + static/ui mount
  settings.py
  aquira/
    client.py             # session, cookies, retry on 401, envelope check
    fieldvalues.py        # unwrap / sparse wrap
    clients.py
    contracts.py
    lookups.py
  hubspot/
    client.py
    properties.py         # ensure aquira_* and custom object schema
    companies.py
    contacts.py
    deals.py
    revenue_periods.py
    owners.py             # list CRM owners
  mapping/
    parties.py            # account vs advertiser vs both
    revenue.py            # monthly allocation
    owners.py             # DB map + suggest-by-email/name
  sync/
    clients.py
    contracts.py
    writeback.py
    orchestrator.py
  db/
    models.py
    repo.py
  webhooks/
    hubspot.py
  jobs/
    poll.py
  ui/
    auth.py
    routes.py
    templates/            # login, dashboard, settings, owners, runs, run_detail, logs
    static/
tests/
  test_fieldvalues.py
  test_parties.py
  test_revenue_allocation.py
  test_idempotency.py
  test_owner_suggest.py
  fixtures/
docker-compose.yml
Dockerfile
README.md                 # deploy on Portainer, env, first-run UI
config/salesrep_owner_map.yaml.example
```

---

## First-run HubSpot bootstrap

On startup if `BOOTSTRAP_HUBSPOT=true` (default true in whatif it only logs):

- Ensure company/contact/deal properties listed above.
- Ensure custom object `revenue_period` + associations to deals and companies.
- Ensure deal pipeline stages exist or map configured stage IDs.
- Fail startup with a clear error if the token lacks scopes: `crm.objects.companies.read/write`, contacts, deals, custom objects, owners.

---

## Acceptance criteria

Must pass:

1. What-if against fixtures produces create/update plans for: account company, advertiser child, contacts, deal, N revenue_period rows.
2. Monthly allocation: $12,000 flight Jan 15–Mar 15 with no spot weights → Jan/Feb/Mar amounts sum to 12000 and Jan/Mar are not empty. Assert exact method in test docstring.
3. Booked Aquira contract maps to HubSpot dealstage closed-won; proposal does not.
4. Re-running the same fixture does not emit HubSpot writes when hashes match.
5. HubSpot company name change in writeback path produces a sparse Aquira Client Put in plan mode.
6. New HubSpot company without aquira_id produces Aquira Client/Create in plan mode.
7. 401 from Aquira triggers one re-login and retry.
8. Docker compose up exposes `/health`, `/ui/login`, and runs the scheduler.
9. README documents Portainer stack deploy, volumes, UI login, and required HubSpot scopes.
10. No secrets in git. `.env.example` only.
11. Settings page Test Aquira / Test HubSpot return a structured pass/fail without writing CRM/traffic data.
12. Owner Suggest maps two fixture pairs by email and one by name; unmatched stay empty.
13. Saved `owner_map` is what the deal-owner mapper uses (not the example YAML).
14. Dashboard what-if toggle persists and the next scheduled job is plan-only when enabled.
15. Force live sync with toggle ON is blocked until confirm; with toggle OFF it runs writes.
16. A what-if run stores `sync_run_item` diffs; `/ui/runs/{id}` renders `field: old → new` for at least one fixture update.

Out of scope for this build: Stripe charges, Aquira log editing, creating Aquira contracts from HubSpot deals, media file transfer.

---

## Locked decisions (do not reopen in code)

These replace “figure it out later.” Fixtures live in `tests/fixtures/`.

### 1. FieldValue + entity fixtures

Access int (locked):

| Access | Meaning | Writable? |
|---|---|---|
| 0 | None | no |
| 1 | ReadOnly | no |
| 2 | ReadAndWrite | yes |

`unwrap(field)` returns `field["Value"]` if dict has `Value`, else the raw value. Missing/null → `None`.

`sparse_put(entity, allowed_paths)` emits only paths whose Access==2.

`tests/fixtures/fieldvalue_client.json` — Client Load envelope:

```json
{
  "Success": true,
  "Error": 0,
  "ErrorName": "",
  "Entity": {
    "ID": 101,
    "Version": 7,
    "Name": "ACME Agency",
    "LongName": "ACME Media Agency",
    "IsAccount": { "Value": true, "Valid": true, "Access": 1 },
    "IsAdvertiser": { "Value": false, "Valid": true, "Access": 1 },
    "IsCurrent": { "Value": true, "Valid": true, "Access": 1 },
    "ShortName": { "Value": "ACME", "Valid": true, "Label": "Short", "Access": 2 },
    "Email": { "Value": "billing@acme.example", "Valid": true, "Access": 2 },
    "Phone": { "Value": "2145550100", "Valid": true, "Access": 2 },
    "Website": { "Value": "acme.example", "Valid": true, "Access": 2 },
    "PhysicalAddress": {
      "Value": {
        "Address": { "Value": { "Value": "100 Main St" }, "Access": 2 },
        "City": { "Value": { "Value": "Dallas" }, "Access": 2 },
        "Region": { "Value": { "Value": "TX" }, "Access": 2 },
        "PostalCode": { "Value": { "Value": "75201" }, "Access": 2 },
        "Country": { "Value": { "Value": "US" }, "Access": 2 }
      },
      "Access": 2
    },
    "Contacts": {
      "Value": [
        {
          "ID": 501,
          "FirstName": { "Value": "Pat", "Access": 2 },
          "LastName": { "Value": "Lee", "Access": 2 },
          "Email": { "Value": "pat@acme.example", "Access": 2 },
          "Phone": { "Value": "2145550199", "Access": 2 }
        }
      ],
      "Access": 1
    }
  }
}
```

`tests/fixtures/fieldvalue_advertiser.json` — Client 202, IsAdvertiser=true, IsAccount=false, Name=`Morning Show`, no email.

`tests/fixtures/fieldvalue_both.json` — Client 303, both flags true (self-represented advertiser).

`tests/fixtures/contract_booked.json` — Contract Load (needed fields only):

```json
{
  "Success": true,
  "Error": 0,
  "Entity": {
    "ID": 9001,
    "Version": 3,
    "Name": "9001",
    "ContractCD": { "Value": "C-9001", "Access": 1 },
    "Status": { "Value": 2, "Access": 1 },
    "IsProposal": { "Value": false, "Access": 1 },
    "IsContract": { "Value": true, "Access": 1 },
    "SignDate": { "Value": "2026-01-05T00:00:00", "Access": 1 },
    "Advertiser": { "Value": { "ID": 202, "Name": "Morning Show", "IsAdvertiser": true }, "Access": 1 },
    "Account": { "Value": { "ID": 101, "Name": "ACME Agency", "IsAccount": true }, "Access": 1 },
    "SalesReps": {
      "Value": [{ "SalesRepID": { "ID": 44, "Name": "Jordan Reyes", "SalesRepID": 44 }, "Selected": true }],
      "Access": 1
    }
  }
}
```

Status int (locked): `0=draft, 1=proposal, 2=booked, 3=cancelled`. Unknown → treat as proposal if IsProposal else booked if IsContract else skip + log.

`tests/fixtures/spotlines_even.json` — normalized internal shape after adapters (not raw swagger dump):

```json
{
  "contract_id": 9001,
  "contract_cd": "C-9001",
  "kind": "booked",
  "fallback_start": "2026-01-15",
  "fallback_end": "2026-03-15",
  "fallback_amount": 12000,
  "lines": [
    {
      "line_id": 1,
      "station_id": 10,
      "station": "KCBI",
      "start": "2026-01-15",
      "end": "2026-03-15",
      "amount": 12000,
      "spots_by_month": null,
      "seconds_by_month": null
    }
  ]
}
```

`tests/fixtures/spotlines_weighted.json`:

```json
{
  "contract_id": 9002,
  "contract_cd": "C-9002",
  "kind": "booked",
  "fallback_start": "2026-01-01",
  "fallback_end": "2026-02-28",
  "fallback_amount": 1000,
  "lines": [
    {
      "line_id": 1,
      "station_id": 10,
      "station": "KCBI",
      "start": "2026-01-01",
      "end": "2026-02-28",
      "amount": 1000,
      "spots_by_month": { "2026-01": 30, "2026-02": 10 }
    }
  ]
}
```

`tests/fixtures/spotlines_missing.json` — `lines: []` so allocator uses fallback_start/end/amount.

Adapter rule: `contracts.normalize_spotlines(raw_load, raw_detail)` must produce this shape. If live Aquira fields differ, map them in the adapter only; tests use the normalized fixtures.

### 2. HubSpot property + custom object manifest

Internal names are exact. Create via Properties API / Schemas API if missing. Do not rename.

**company** (`aquira_*` group `Aquira`):

| name | type | fieldType | unique |
|---|---|---|---|
| aquira_id | number | number | yes |
| aquira_version | number | number | no |
| aquira_party_type | enumeration | select | no — options `account`, `advertiser`, `both` |
| aquira_is_account | bool | booleancheckbox | no |
| aquira_is_advertiser | bool | booleancheckbox | no |
| aquira_client_cd | string | text | no |

Standard used as-is: `name`, `domain`, `phone`, `address`, `city`, `state`, `zip`, `country`, `website`.

**contact:** `aquira_id` (string, unique, format `{clientId}:{contactId}`), `aquira_client_id` (number). Standard: `firstname`, `lastname`, `email`, `phone`.

**deal:**

| name | type |
|---|---|
| aquira_id | number, unique |
| aquira_version | number |
| aquira_contract_cd | string |
| aquira_status | number |
| aquira_is_proposal | bool |
| aquira_is_contract | bool |
| aquira_sign_date | date |
| aquira_start_date | date |
| aquira_end_date | date |
| aquira_stations | string |
| aquira_account_id | number |
| aquira_advertiser_id | number |
| aquira_sales_rep | string |

Standard: `dealname`, `amount`, `closedate`, `pipeline`, `dealstage`, `hubspot_owner_id`.

Pipeline mapping config (IDs filled at runtime / UI later):

```yaml
pipeline: default
stages:
  proposal: proposal
  booked: closedwon
  cancelled: closedlost
```

**custom object `revenue_period`**

- name: `revenue_period`
- labels: singular `Revenue Period`, plural `Revenue Periods`
- primary display: `aquira_id`
- required to create: `aquira_id`, `period`, `amount`
- properties: `aquira_id` (string unique), `period` (date), `amount` (number), `station` (string), `station_id` (number), `kind` (enumeration `proposal`/`booked`), `contract_cd` (string), `contract_id` (number)
- associations: DEAL, COMPANY (use HubSpot-defined types after schema create; store type ids in settings)

Bootstrap is idempotent: GET schema/properties, POST only missing.

### 3. Revenue-period algorithm (exact)

Function: `allocate_revenue(normalized_spotlines) -> list[RevenuePeriod]`.

Month key = first calendar day `YYYY-MM-01`. A line “touches” a month if its `[start, end]` inclusive overlaps that month at all.

**Even split (spots_by_month and seconds_by_month both empty):**
- `months = touched_months(start, end)`
- each month gets `round_cents(amount / n)` except the last month which gets remainder so the line sums exactly to `amount`.

Worked example A — `spotlines_even.json`:
- touches Jan, Feb, Mar (n=3)
- 12000 / 3 = 4000.00 each
- emit:
  - `9001:2026-01:10` KCBI 4000 booked
  - `9001:2026-02:10` KCBI 4000 booked
  - `9001:2026-03:10` KCBI 4000 booked

**Weighted split:** if `spots_by_month` present, weight = spots in that month; else if `seconds_by_month`, weight = seconds. Months with weight 0 get 0. Remainder cents go to the last month with weight > 0.

Worked example B — `spotlines_weighted.json`:
- weights 30 / 10, amount 1000
- Jan 750.00, Feb 250.00
- `9002:2026-01:10` = 750, `9002:2026-02:10` = 250

**Fallback:** `lines == []` → one synthetic line using fallback_start, fallback_end, fallback_amount, station_id=0, station=`ALL`.

Worked example C — Jan 15–Mar 15, 12000, no lines → three periods station_id=0 at 4000 each. Ids `9001:2026-01:0` etc.

**Multi-line:** allocate each line, then sum amounts for the same `(contract_id, period, station_id)`.

**Stale delete:** current ids for contract C minus previous ids for C → delete those HubSpot records (what-if: action `delete-stale`).

Money: Decimal, 2 places, banker's rounding only on intermediate month slices; last slice absorbs remainder.

### 4. Failing tests to write first

| Test file | Assert |
|---|---|
| `test_fieldvalues.py` | unwrap nested address to `"100 Main St"`; sparse_put on client fixture includes Email/Phone/ShortName and excludes IsAccount; Access 1 dropped |
| `test_revenue_allocation.py` | examples A, B, C exact amounts and aquira_id keys; two lines same station/month sum; stale set difference |
| `test_owner_suggest.py` | email exact wins; name case-insensitive; no match → None; email beats conflicting name |
| `test_whatif_diff.py` | `diff_props(old, new)` → `[{field, from, to}]`; unchanged omitted; what-if planner emits create/update/skip without calling write mocks |
| `test_parties.py` | 101+202 → account parent + advertiser child; 303 → single `both` |
| `test_lock_retry.py` | second `run_sync` while lock held raises `SyncInProgress`; 401 retries login once; 3rd HubSpot 429 then success; after max fail → dead_letter row |
| `test_webhook_dedupe.py` | valid v3 signature accepted; bad signature 401; replay of same `messageId` within 24h is no-op |

### 5. Sync lock, retry, dead-letter

- Lock: Postgres advisory lock `pg_advisory_lock(90091)` or a `sync_lock` row with `locked_at` + owner token. TTL 30 minutes; steal if expired.
- One active `sync_run` at a time. UI buttons disabled while lock held.
- Retry: Aquira HTTP 401 → re-login once, retry that request. Aquira 5xx / timeout → 3 attempts, backoff 2s, 6s, 18s. HubSpot 429 → honor `Retry-After` (cap 60s), max 5 tries. HubSpot 5xx → 3 tries same backoff.
- Per-entity failure does not abort the run. Append `sync_run_item` error + `dead_letter` (entity_type, aquira_id, error, payload_json, attempts).
- Dead-letter retry: next scheduled run retries items with `attempts < 5`. After 5, leave for manual Force sync of that id.
- Run status: `success` if no item errors; `partial` if some errors; `failed` if lock/login/bootstrap died before items.

### 6. HubSpot webhook verify + dedupe

- Endpoint `POST /webhooks/hubspot`.
- Signature: HubSpot v3 — `X-HubSpot-Signature-v3` = Base64(HMACSHA256(client_secret, method + uri + body + timestamp)). Reject if `X-HubSpot-Request-Timestamp` older than 5 minutes or signature mismatch → 401.
- Dedup table `webhook_event(message_id PK, received_at, subscription_type)`. If `messageId` exists → 200 empty, no work.
- Handle `company.propertyChange`, `contact.propertyChange` for identity fields only (`name`, `domain`, `phone`, `address`, `city`, `state`, `zip`, `website`, `firstname`, `lastname`, `email`). Other properties ignored.
- Enqueue writeback by HubSpot record id; do not write Aquira inline on the request thread beyond inserting the event row (respond 200 quickly).
- No HubSpot deal webhooks in v1.

---

## Implementation notes for Codex

- Aquira FieldValue `Access` is an int; treat `ReadAndWriteAccess` as whatever the live API returns on a writable field. Detect empirically in what-if by loading one client and printing access values once in a debug helper.
- Contract models are large. Map only needed fields; keep raw JSON on `id_map` or a `raw_snapshot` table for debugging (truncate/rotate).
- Search may be contains-only and unpaginated poorly. Implement defensive paging if response includes `NumberOfResults`. If Search cannot list all, use AdvancedSearch filters by modified date when the API allows; otherwise full scan + hash skip.
- Be conservative on rate limits: serialize Aquira calls (one session), batch HubSpot (batch upsert 100).
- Comments only where the Aquira quirk is non-obvious (cookies, sparse put, monthly allocation).
- Deliver working compose, tests, and README last so the project is runnable.

Start: drop fixtures from Locked decisions → write the failing tests in section 4 → implement FieldValue, allocate_revenue, owner suggest, diff, lock/retry, webhook verify until green → then adapters, orchestrator, UI, Docker.
