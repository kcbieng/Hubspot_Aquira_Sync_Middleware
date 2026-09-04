# HubQuira Portainer deploy

This stack is meant to be deployed from GitHub with Portainer **Stacks → Add stack → Repository**.

Repository: `https://github.com/kcbieng/Hubspot_Aquira_Sync_Middleware`  
Compose path: `docker-compose.yml`  
Branch: `main`

## Private repository access

If the repo is private, Portainer needs GitHub credentials:

1. GitHub → Settings → Developer settings → Personal access tokens
2. Create a classic token with `repo` scope (or a fine-grained token with Contents: Read)
3. In the Portainer stack editor, enable repository authentication
4. Username: your GitHub username  
   Password: the token (not your GitHub password)

Portainer clones the repo on the Docker host and **builds** `middleware` from `Dockerfile`. There is no image on Docker Hub. `docker pull` / Portainer **Re-pull image** will not pick up GitHub commits.

The stack has two HubQuira containers from the same image:

- `middleware` — operator UI and webhooks only (`HUBQUIRA_ROLE=web`)
- `worker` — catalog pull, HubSpot writes, scheduled poll (`HUBQUIRA_ROLE=worker`)

A sync must never run inside the UI process. If the console still freezes during a run, the worker container is not up.

## Updating after a GitHub push

1. Portainer → Stacks → this stack
2. **Pull and redeploy** (this git-pulls `main`)
3. Enable **Re-build image** if the checkbox is shown. **Re-pull image** is for registry images and does nothing here.
4. Enable **Force recreation of containers** / **Re-create containers** if present
5. Open the operator Dashboard and confirm **Build** is `2026.09.04-client-fullname` (or newer)

If the build line does not change, Portainer reused the old local image. On the Docker host:

```bash
# Portainer git stacks usually land under /data/compose/<stack-id>
cd /data/compose/<stack-id>
docker compose build --no-cache middleware
docker compose up -d middleware
```

`docker-compose.yml` sets `pull_policy: build` so Compose builds from the Dockerfile instead of trying to pull `aquira-hubspot-middleware:latest`.

## Environment

Copy [`stack.env.example`](stack.env.example) into the Portainer **Environment variables** editor.

Required:

| Variable | Purpose |
|---|---|
| `AQUIRA_USERNAME` / `AQUIRA_PASSWORD` | Aquira service user |
| `HUBSPOT_ACCESS_TOKEN` | HubSpot private app token |
| `POSTGRES_PASSWORD` | Postgres password |
| `DATABASE_URL` | Must use the same password: `postgresql://middleware:<POSTGRES_PASSWORD>@db:5432/aquira_hubspot` |
| `SETTINGS_FERNET_KEY` | Fernet key for secrets stored from the UI |
| `UI_PASSWORD` | Operator login password |

Recommended:

| Variable | Default | Notes |
|---|---|---|
| `WHATIF` | `true` | Keep true until a what-if run looks right |
| `PUBLIC_BASE_URL` | empty | `https://sync.example.com` so HubSpot webhook signatures verify behind a reverse proxy |
| `HUBSPOT_CLIENT_SECRET` | empty | App secret from the HubSpot webhook subscription |
| `SYNC_INTERVAL_MINUTES` | `30` | Scheduler interval |
| `AQUIRA_TEAM_ATTRIBUTE` | `HubSpot Team` | Aquira custom attribute whose value is the exact HubSpot team name |
| `MIDDLEWARE_PORT` | `8080` | Host port published by the stack |

Generate the Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## HubSpot private app scopes

Enable at least:

- `crm.objects.companies.read` / `crm.objects.companies.write`
- `crm.objects.contacts.read` / `crm.objects.contacts.write`
- `crm.objects.deals.read` / `crm.objects.deals.write`
- `crm.objects.owners.read`
- `settings.users.read` (optional but recommended — labels Super Admins vs sales roles, and lists HubSpot teams for contact/company/deal team assignment)
- `crm.schemas.companies.write` / `crm.schemas.contacts.write` / `crm.schemas.deals.write` (schema bootstrap)
- Custom objects / schemas if you want `revenue_period` created automatically

Webhook URL: `{PUBLIC_BASE_URL}/webhooks/hubspot`

Aquira notification URL: `{PUBLIC_BASE_URL}/webhooks/aquira?token=<AQUIRA_WEBHOOK_SECRET>`

Subscribe HubSpot to:

- `company.propertyChange` (name, phone, domain, website, address)
- `contact.propertyChange` (firstname, lastname, email, phone)
- `company.creation` if new HubSpot companies should become Aquira clients

Aquira does not publish a webhook payload schema. Point the user-notification types (proposal submitted/accepted/rejected, contract created/modified, spotlines/charges changed) at the HubQuira URL. The raw body is written to **Logs**; if a contract or proposal id can be parsed, that deal is synced immediately.

## First-run sequence

1. Deploy with `WHATIF=true`
2. Open `http://<host>:8080/ui/login`
3. Settings → Test Aquira and Test HubSpot. Both must return `status: ok`
4. Owners → Suggest matches → save
5. Teams → Suggest matches → save. Aquira custom attribute default name is `HubSpot Team` (override with `AQUIRA_TEAM_ATTRIBUTE`); its value must be the exact HubSpot team name.
6. Dashboard → Run what-if now
7. Open the run and read field diffs
8. Type `WRITE` and Force live sync only after that plan is correct
9. Turn what-if off when the 30-minute job should write

`/health` is the container healthcheck. `/ready` always returns HTTP 200 for process liveness and includes `aquira_configured` / `hubspot_configured` flags.

## Updates

In Portainer, **Pull and redeploy** the stack after pushing to `main`. Named volumes `postgres_data` and `app_logs` are preserved.

Do not expose Postgres on a host port. The middleware talks to it on the internal Docker network.
