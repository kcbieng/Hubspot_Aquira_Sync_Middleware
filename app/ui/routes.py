import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.routes import aquira_owners, hubspot_owners, hubspot_teams, owner_map, team_map
from app.db.models import OwnerMap, TeamMap
from app.db.repo import Repo
from app.runtime import persist_settings
from app.session import is_logged_in, set_session
from app.settings import get_settings
from app.sync.orchestrator import SyncContext, SyncOrchestrator
from app.version import REVISION

router = APIRouter(prefix="/ui", tags=["ui"])
templates = Jinja2Templates(directory="app/ui/templates")


def _require_login(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/ui/login", status_code=303)
    return None


def _page(request: Request, name: str, context: dict):
    context.setdefault("settings", get_settings())
    context.setdefault("revision", REVISION)
    return templates.TemplateResponse(request, name, context)


def _latest_sync_output() -> dict[str, object]:
    repo = Repo()
    run = repo.latest_run()
    items: list[dict[str, object]] = []
    if run is not None:
        rows = repo.list_run_items(run.id)[-10:]
        for row in reversed(rows) if False else rows:
            payload = None
            if row.diff_json:
                try:
                    payload = json.loads(row.diff_json)
                except json.JSONDecodeError:
                    payload = {"raw": row.diff_json}
            items.append(
                {
                    "entity_type": row.entity_type,
                    "action": row.action,
                    "mode": "what-if" if run.whatif else "live",
                    "payload": payload,
                    "aquira_id": row.aquira_id,
                    "hubspot_id": row.hubspot_id,
                    "error": row.error,
                }
            )
        items = list(reversed(items))[:10]
    return {"run": run, "items": items}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _page(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    settings = get_settings()
    if username == settings.ui_username and password == settings.ui_password:
        response = RedirectResponse(url="/ui", status_code=303)
        set_session(response)
        return response
    return _page(request, "login.html", {"error": "Invalid credentials"})


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    settings = get_settings()
    repo = Repo()
    cursor = repo.get_cursor("poll")
    latest = repo.latest_run()
    return _page(
        request,
        "dashboard.html",
        {
            "mode_label": "PLAN ONLY — no writes" if settings.whatif else "LIVE WRITES",
            "status": "ready",
            "recent_sync_output": _latest_sync_output(),
            "aquira_configured": bool(settings.aquira_username and settings.aquira_password),
            "hubspot_configured": bool(settings.hubspot_access_token),
            "last_success_at": cursor.last_success_at if cursor else None,
            "last_error": cursor.last_error if cursor else None,
            "latest": latest,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    return _page(request, "settings.html", {"error": None, "notice": None})


@router.post("/settings")
async def update_settings(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect

    form = await request.form()
    if "aquira_base_url" in form:
        payload = {
            "whatif": str(form.get("whatif", "false")).lower() in {"1", "true", "on", "yes"},
            "sync_interval_minutes": form.get("sync_interval_minutes"),
            "aquira_base_url": form.get("aquira_base_url"),
            "aquira_username": form.get("aquira_username"),
            "aquira_password": form.get("aquira_password"),
            "hubspot_access_token": form.get("hubspot_access_token"),
            "hubspot_client_secret": form.get("hubspot_client_secret"),
            "ui_username": form.get("ui_username"),
            "ui_password": form.get("ui_password"),
            "sync_calls": str(form.get("sync_calls", "false")).lower() in {"1", "true", "on", "yes"},
            "sync_create_aquira_client": str(form.get("sync_create_aquira_client", "false")).lower() in {"1", "true", "on", "yes"},
            "bootstrap_hubspot": str(form.get("bootstrap_hubspot", "false")).lower() in {"1", "true", "on", "yes"},
            "aquira_team_attribute": form.get("aquira_team_attribute") or "HubSpot Team",
        }
        try:
            payload["sync_interval_minutes"] = int(payload["sync_interval_minutes"] or 30)
        except (TypeError, ValueError):
            payload["sync_interval_minutes"] = 30
        persist_settings({key: value for key, value in payload.items() if value is not None})
        return RedirectResponse(url="/ui/settings", status_code=303)

    settings = get_settings()
    settings.whatif = str(form.get("whatif", "false")).lower() in {"1", "true", "on", "yes"}
    try:
        settings.sync_interval_minutes = int(form.get("sync_interval_minutes", settings.sync_interval_minutes))
    except ValueError:
        settings.sync_interval_minutes = 30
    persist_settings({"whatif": settings.whatif, "sync_interval_minutes": settings.sync_interval_minutes})
    return RedirectResponse(url="/ui", status_code=303)


@router.get("/owners", response_class=HTMLResponse)
def owners_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    repo = Repo()
    rows = repo.list_owner_maps()
    if not rows:
        try:
            owner_map()
            rows = repo.list_owner_maps()
        except Exception:
            rows = []
    return _page(
        request,
        "owners.html",
        {
            "rows": rows,
            "aquira_reps": aquira_owners(),
            "hubspot_owners": hubspot_owners(),
        },
    )


@router.post("/owners")
async def owners_save(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    form = await request.form()
    repo = Repo()
    action = str(form.get("action") or "save")
    if action == "suggest":
        owner_map()
        return RedirectResponse(url="/ui/owners", status_code=303)
    aquira_ids = form.getlist("aquira_user_id")
    for aquira_id in aquira_ids:
        owner_id = str(form.get(f"hubspot_owner_id_{aquira_id}") or "") or None
        enabled = str(form.get(f"enabled_{aquira_id}") or "") in {"1", "on", "true"}
        row = repo.session.get(OwnerMap, str(aquira_id))
        if row is None:
            continue
        hubspot = next((item for item in hubspot_owners() if item.get("owner_id") == owner_id), None)
        row.hubspot_owner_id = owner_id
        row.hubspot_name = (hubspot or {}).get("name")
        row.hubspot_email = (hubspot or {}).get("email")
        row.enabled = enabled and bool(owner_id)
        row.suggested = False
        repo.session.add(row)
    repo.session.commit()
    return RedirectResponse(url="/ui/owners", status_code=303)


@router.get("/teams", response_class=HTMLResponse)
def teams_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    repo = Repo()
    rows = repo.list_team_maps()
    if not rows:
        try:
            team_map()
            rows = repo.list_team_maps()
        except Exception:
            rows = []
    return _page(
        request,
        "teams.html",
        {
            "rows": rows,
            "hubspot_teams": hubspot_teams(),
            "attribute_name": get_settings().aquira_team_attribute or "HubSpot Team",
        },
    )


@router.post("/teams")
async def teams_save(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    form = await request.form()
    repo = Repo()
    action = str(form.get("action") or "save")
    if action == "suggest":
        team_map()
        return RedirectResponse(url="/ui/teams", status_code=303)
    aquira_keys = form.getlist("aquira_key")
    teams = hubspot_teams()
    for aquira_key in aquira_keys:
        team_id = str(form.get(f"hubspot_team_id_{aquira_key}") or "") or None
        enabled = str(form.get(f"enabled_{aquira_key}") or "") in {"1", "on", "true"}
        row = repo.session.get(TeamMap, str(aquira_key))
        if row is None:
            continue
        hubspot = next((item for item in teams if str(item.get("id")) == str(team_id or "")), None)
        row.hubspot_team_id = team_id
        row.hubspot_team_name = (hubspot or {}).get("name")
        row.enabled = enabled and bool(team_id)
        row.suggested = False
        repo.session.add(row)
    repo.session.commit()
    return RedirectResponse(url="/ui/teams", status_code=303)


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    repo = Repo()
    return _page(request, "runs.html", {"runs": repo.list_runs(50)})


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(request: Request, run_id: int):
    redirect = _require_login(request)
    if redirect:
        return redirect
    repo = Repo()
    run = repo.get_run(run_id)
    items = repo.list_run_items(run_id) if run is not None else []
    parsed = []
    for item in items:
        diff = None
        if item.diff_json:
            try:
                diff = json.loads(item.diff_json)
            except json.JSONDecodeError:
                diff = {"raw": item.diff_json}
        parsed.append({"row": item, "diff": diff})
    return _page(request, "run_detail.html", {"run": run, "items": items, "parsed": parsed})


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    repo = Repo()
    rows = repo.list_events(200)
    return _page(request, "logs.html", {"events": rows})


def _execute_sync(whatif: bool, trigger: str = "manual", aquira_id: str | None = None) -> RedirectResponse:
    from app.db.repo import Repo

    repo = Repo()
    try:
        result = SyncOrchestrator().run(
            SyncContext(trigger=trigger, whatif=whatif, aquira_id=aquira_id or None),
            repo=repo,
        )
    finally:
        repo.close()
    run_id = result.get("run_id")
    if run_id:
        return RedirectResponse(url=f"/ui/runs/{run_id}", status_code=303)
    return RedirectResponse(url="/ui", status_code=303)


@router.post("/sync/run")
async def run_sync(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    form = await request.form()
    settings = get_settings()
    force_live = str(form.get("force_live") or "").lower() in {"1", "true", "on", "yes"}
    confirm = str(form.get("confirm") or "")
    whatif_override = form.get("whatif")
    if force_live:
        if settings.whatif and confirm != "WRITE":
            return _page(
                request,
                "dashboard.html",
                {
                    "mode_label": "PLAN ONLY — no writes",
                    "status": "ready",
                    "recent_sync_output": _latest_sync_output(),
                    "error": "Type WRITE to force a live sync while plan-only mode is on.",
                    "aquira_configured": bool(settings.aquira_username and settings.aquira_password),
                    "hubspot_configured": bool(settings.hubspot_access_token),
                },
            )
        whatif = False
    elif whatif_override is not None:
        whatif = str(whatif_override).lower() in {"1", "true", "on", "yes"}
    else:
        whatif = settings.whatif
    aquira_id = str(form.get("aquira_id") or "") or None
    try:
        return _execute_sync(whatif=whatif, trigger="manual", aquira_id=aquira_id)
    except SyncInProgress:
        return RedirectResponse(url="/ui", status_code=303)
