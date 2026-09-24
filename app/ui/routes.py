import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.routes import aquira_owners, hubspot_owners, hubspot_teams, owner_map, team_map
from app.auth import hash_password, verify_password
from app.db.models import AppUser, MatchRule, OwnerMap, TeamMap
from app.db.repo import Repo
from app.runtime import persist_settings
from app.session import (
    clear_session,
    cookie_params,
    is_logged_in,
    session_identity,
    set_session,
    set_user_session,
)
from app.settings import get_settings
from app import sso as sso_module
from app.version import REVISION

router = APIRouter(prefix="/ui", tags=["ui"])
templates = Jinja2Templates(directory="app/ui/templates")

MATCH_MODE_CHOICES = [
    {"name": "domain", "label": "Website domain (normalized)"},
    {"name": "normalized", "label": "Name (legal suffixes stripped)"},
    {"name": "exact", "label": "Exact text (case-insensitive)"},
    {"name": "email", "label": "Email"},
    {"name": "phone", "label": "Phone (last 10 digits)"},
    {"name": "contains", "label": "Contains (either side)"},
]
MATCH_AQUIRA_FIELDS = {
    "company": [
        {"name": "Name", "label": "Name"},
        {"name": "LongName", "label": "Long name"},
        {"name": "ShortName", "label": "Short name"},
        {"name": "ClientCD", "label": "Client CD (UI number)"},
        {"name": "Website", "label": "Website"},
        {"name": "Email", "label": "Email"},
        {"name": "Phone", "label": "Phone"},
        {"name": "PhysicalAddress", "label": "Street"},
        {"name": "City", "label": "City"},
        {"name": "State", "label": "State"},
    ],
    "contact": [
        {"name": "FirstName", "label": "First name"},
        {"name": "LastName", "label": "Last name"},
        {"name": "Email", "label": "Email"},
        {"name": "Phone", "label": "Phone"},
        {"name": "ClientID", "label": "Aquira client ID"},
    ],
}
_DEFAULT_HUBSPOT_FIELDS = {
    "company": ["name", "domain", "website", "phone", "city", "state", "address"],
    "contact": ["firstname", "lastname", "email", "phone"],
}


def _hubspot_field_choices(entity_type: str) -> list[dict[str, str]]:
    """Live property list from the portal — the dropdown shows the fields the
    matching can actually read; offline/unconfigured falls back to core."""
    object_type = "companies" if entity_type == "company" else "contacts"
    try:
        from app.hubspot.client import HubSpotClient

        rows = HubSpotClient().get_properties(object_type).get("results") or []
        out = [
            {"name": str(r.get("name")), "label": f"{r.get('label') or r.get('name')} ({r.get('name')})"}
            for r in rows
            if r.get("name")
        ]
        if out:
            return sorted(out, key=lambda c: c["name"].lower())
    except Exception:
        pass
    return [{"name": n, "label": n} for n in _DEFAULT_HUBSPOT_FIELDS[entity_type]]


def _require_login(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/ui/login", status_code=303)
    return None


def _require_admin(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    if str((session_identity(request) or {}).get("role") or "") != "admin":
        return HTMLResponse(
            "<h2>403 — Admin access required</h2><p><a href='/ui/matches'>Go to your match reviews</a></p>",
            status_code=403,
        )
    return None


def _page(request: Request, name: str, context: dict):
    context.setdefault("settings", get_settings())
    context.setdefault("revision", REVISION)
    identity = session_identity(request) or {}
    context.setdefault("role", identity.get("role") or "")
    context.setdefault("user_email", identity.get("email") or "")
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


SSO_STATE_COOKIE = "sso_state"


def _sso_error(request: Request, exc: Exception) -> HTMLResponse:
    return HTMLResponse(
        "<h2>Single sign-on failed</h2>"
        f"<p>{str(exc)[:300]}</p>"
        "<p><a href='/ui/login'>Back to login</a> (local admin sign-in always works).</p>",
        status_code=403,
    )


@router.get("/sso")
def sso_start(request: Request):
    identity = session_identity(request)
    if identity:
        return RedirectResponse(url="/ui", status_code=303)
    if not sso_module.sso_ready():
        return RedirectResponse(url="/ui/login", status_code=303)
    try:
        url, cookie = sso_module.begin_login()
    except sso_module.SsoError as exc:
        return _sso_error(request, exc)
    response = RedirectResponse(url, status_code=302)
    params = cookie_params()
    response.set_cookie(
        SSO_STATE_COOKIE, cookie, httponly=True, samesite="lax",
        secure=bool(params.get("secure")), max_age=sso_module.STATE_TTL_SECONDS, path="/",
    )
    return response


@router.get("/sso/callback")
def sso_callback(request: Request, code: str = "", state: str = ""):
    cookie = request.cookies.get(SSO_STATE_COOKIE) or ""
    try:
        asserted = sso_module.complete_login(code, state, cookie)
    except sso_module.SsoError as exc:
        return _sso_error(request, exc)
    repo = Repo()
    try:
        user, allowed = repo.provision_sso_user(
            asserted["email"], asserted["name"], asserted["role"], asserted["subject"]
        )
    finally:
        try:
            repo.close()
        except Exception:
            pass
    if not allowed:
        return _sso_error(request, RuntimeError(f"account {asserted['email']} is disabled in HubQuira"))
    response = RedirectResponse(url="/ui", status_code=303)
    set_user_session(response, asserted["email"], asserted["role"])
    response.delete_cookie(SSO_STATE_COOKIE, path="/")
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _page(request, "login.html", {"error": None, "sso_available": sso_module.sso_ready()})


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
    repo = Repo()
    try:
        user = repo.get_user(username)
        # copy what the redirect needs — the session closes below and would
        # expire the ORM instance before the attribute reads after it
        identity = (user.email, user.role, user.password_hash) if user is not None else None
    finally:
        try:
            repo.close()
        except Exception:
            pass
    if identity and identity[2] and verify_password(password, identity[2]):
        response = RedirectResponse(url="/ui/matches", status_code=303)
        set_user_session(response, identity[0], identity[1])
        return response
    return _page(request, "login.html", {"error": "Invalid credentials", "sso_available": sso_module.sso_ready()})


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
    redirect = _require_admin(request)
    if redirect:
        return redirect
    return _page(request, "settings.html", {"error": None, "notice": None})


@router.post("/settings")
async def update_settings(request: Request):
    redirect = _require_admin(request)
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
            "aquira_webhook_secret": form.get("aquira_webhook_secret"),
            "ui_username": form.get("ui_username"),
            "ui_password": form.get("ui_password"),
            "sync_calls": str(form.get("sync_calls", "false")).lower() in {"1", "true", "on", "yes"},
            "sync_writeback": str(form.get("sync_writeback", "false")).lower() in {"1", "true", "on", "yes"},
            "sync_create_aquira_client": str(form.get("sync_create_aquira_client", "false")).lower() in {"1", "true", "on", "yes"},
            "bootstrap_hubspot": str(form.get("bootstrap_hubspot", "false")).lower() in {"1", "true", "on", "yes"},
            "aquira_team_attribute": form.get("aquira_team_attribute") or "HubSpot Team",
            "smtp_host": form.get("smtp_host"),
            "smtp_port": form.get("smtp_port"),
            "smtp_user": form.get("smtp_user"),
            "smtp_password": form.get("smtp_password"),
            "smtp_from": form.get("smtp_from"),
            "match_digest_enabled": str(form.get("match_digest_enabled", "false")).lower() in {"1", "true", "on", "yes"},
            "sso_enabled": str(form.get("sso_enabled", "false")).lower() in {"1", "true", "on", "yes"},
            "oidc_issuer": form.get("oidc_issuer"),
            "oidc_client_id": form.get("oidc_client_id"),
            "oidc_client_secret": form.get("oidc_client_secret"),
            "sso_admin_group": form.get("sso_admin_group"),
            "sso_sales_group": form.get("sso_sales_group"),
            "teams_webhook_url": form.get("teams_webhook_url"),
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
    redirect = _require_admin(request)
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
    redirect = _require_admin(request)
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
    redirect = _require_admin(request)
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
            "hubspot_owners": hubspot_owners(),
            "attribute_name": get_settings().aquira_team_attribute or "Hubspot_Team",
        },
    )


@router.post("/teams")
async def teams_save(request: Request):
    redirect = _require_admin(request)
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
    owners = hubspot_owners()
    for aquira_key in aquira_keys:
        team_id = str(form.get(f"hubspot_team_id_{aquira_key}") or "") or None
        owner_id = str(form.get(f"hubspot_owner_id_{aquira_key}") or "") or None
        enabled = str(form.get(f"enabled_{aquira_key}") or "") in {"1", "on", "true"}
        row = repo.session.get(TeamMap, str(aquira_key))
        if row is None:
            continue
        hubspot = next((item for item in teams if str(item.get("id")) == str(team_id or "")), None)
        owner = next((item for item in owners if str(item.get("owner_id")) == str(owner_id or "")), None)
        row.hubspot_team_id = team_id
        row.hubspot_team_name = (hubspot or {}).get("name")
        row.hubspot_owner_id = owner_id
        row.hubspot_owner_name = (owner or {}).get("name")
        row.enabled = enabled and bool(team_id)
        row.suggested = False
        repo.session.add(row)
    repo.session.commit()
    return RedirectResponse(url="/ui/teams", status_code=303)


def _rule_conditions(row) -> list[dict[str, str]]:
    try:
        conditions = json.loads(row.conditions_json or "[]")
    except (TypeError, ValueError):
        conditions = []
    return [c for c in conditions if isinstance(c, dict)]


@router.get("/matching", response_class=HTMLResponse)
def matching_page(request: Request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    repo = Repo()
    repo.ensure_default_match_rules()
    rules = repo.list_match_rules()
    return _page(
        request,
        "matching.html",
        {
            "rules_by_entity": {
                "company": [r for r in rules if r.entity_type == "company"],
                "contact": [r for r in rules if r.entity_type == "contact"],
            },
            "conditions_by_id": {r.id: _rule_conditions(r) for r in rules},
            "aquira_fields": MATCH_AQUIRA_FIELDS,
            "hubspot_fields": {et: _hubspot_field_choices(et) for et in ("company", "contact")},
            "modes": MATCH_MODE_CHOICES,
        },
    )


@router.post("/matching")
async def matching_save(request: Request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    form = await request.form()
    repo = Repo()
    action = str(form.get("action") or "")

    def conditions_from() -> list[dict[str, str]]:
        aq = form.getlist("cond_aquira")
        hs = form.getlist("cond_hubspot")
        md = form.getlist("cond_mode")
        conditions = []
        for a, b, m in zip(aq, hs, md):
            a, b, m = str(a or "").strip(), str(b or "").strip(), str(m or "").strip()
            if a and b and m:
                conditions.append({"aquira_field": a, "hubspot_field": b, "mode": m})
        return conditions

    def rule_id() -> int | None:
        try:
            return int(form.get("rule_id"))
        except (TypeError, ValueError):
            return None

    if action == "create":
        entity_type = str(form.get("entity_type") or "company")
        if entity_type not in {"company", "contact"}:
            entity_type = "company"
        conditions = conditions_from()
        if conditions:
            repo.create_match_rule(
                entity_type,
                str(form.get("name") or "New rule").strip()[:120] or "New rule",
                conditions,
                str(form.get("on_match") or "link"),
            )
        return RedirectResponse(url="/ui/matching", status_code=303)

    rid = rule_id()
    if rid is not None:
        if action == "delete":
            repo.delete_match_rule(rid)
        elif action in {"up", "down"}:
            repo.move_match_rule(rid, action)
        elif action == "update":
            conditions = conditions_from()
            row = repo.session.get(MatchRule, rid)
            if row is None:
                return RedirectResponse(url="/ui/matching", status_code=303)
            repo.update_match_rule(
                rid,
                name=str(form.get("name") or "").strip()[:120] or None,
                conditions=conditions or None,  # an all-blank edit keeps the stored conditions
                on_match=str(form.get("on_match") or "") or None,
                enabled=bool(form.get(f"enabled_{rid}")),
            )
    return RedirectResponse(url="/ui/matching", status_code=303)


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    repo = Repo()
    return _page(request, "runs.html", {"runs": repo.list_runs(50)})


def _suggestions_viewable(request: Request, repo: Repo) -> list:
    identity = session_identity(request) or {}
    rows = repo.list_suggestions(("pending",))
    if identity.get("role") == "admin":
        return list(rows)
    email = str(identity.get("email") or "").lower()
    return [r for r in rows if (r.assignee_email or "").lower() in {email, ""} or not r.assignee_email]


@router.get("/matches", response_class=HTMLResponse)
def matches_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    repo = Repo()
    identity = session_identity(request) or {}
    try:
        exclusions = repo.list_match_exclusions() if identity.get("role") == "admin" else []
        return _page(
            request,
            "matches.html",
            {
                "suggestions": _suggestions_viewable(request, repo),
                "exclusions": exclusions,
                "is_admin": identity.get("role") == "admin",
            },
        )
    finally:
        try:
            repo.close()
        except Exception:
            pass


@router.post("/matches")
async def matches_action(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    form = await request.form()
    repo = Repo()
    identity = session_identity(request) or {}
    action = str(form.get("action") or "")
    entity_type = str(form.get("entity_type") or "")
    aquira_id = str(form.get("aquira_id") or "")
    hubspot_id = str(form.get("hubspot_id") or "")
    if entity_type not in {"company", "contact"} or not aquira_id or not hubspot_id:
        return RedirectResponse(url="/ui/matches", status_code=303)
    obj = "companies" if entity_type == "company" else "contacts"
    try:
        if action == "link":
            from app.hubspot.client import HubSpotClient

            HubSpotClient().upsert_crm(obj, {"aquira_id": aquira_id}, hubspot_id)
            repo.set_suggestion_status(entity_type, aquira_id, hubspot_id, "linked")
            repo.add_event("matches", "INFO", f"linked Aquira {aquira_id} to HubSpot {obj} {hubspot_id}",
                           {"by": identity.get("email")})
        elif action == "dismiss":
            repo.add_match_exclusion(entity_type, aquira_id, hubspot_id, str(identity.get("email") or ""))
            repo.set_suggestion_status(entity_type, aquira_id, hubspot_id, "dismissed")
            repo.add_event("matches", "INFO", f"declared {obj} {hubspot_id} NOT a duplicate of Aquira {aquira_id}",
                           {"by": identity.get("email")})
        elif action == "remove-exclusion" and identity.get("role") == "admin":
            repo.removal_match_exclusion(entity_type, aquira_id, hubspot_id)
    except Exception as exc:
        return _page(
            request,
            "matches.html",
            {
                "suggestions": _suggestions_viewable(request, repo),
                "exclusions": repo.list_match_exclusions() if identity.get("role") == "admin" else [],
                "is_admin": identity.get("role") == "admin",
                "error": f"{action} failed: {exc}",
            },
        )
    finally:
        try:
            repo.close()
        except Exception:
            pass
    return RedirectResponse(url="/ui/matches", status_code=303)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    repo = Repo()
    try:
        return _page(request, "users.html", {"users": repo.list_users()})
    finally:
        try:
            repo.close()
        except Exception:
            pass


@router.post("/users")
async def users_save(request: Request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    form = await request.form()
    repo = Repo()
    action = str(form.get("action") or "")
    try:
        if action == "create":
            email = str(form.get("email") or "").strip().lower()
            password = str(form.get("password") or "")
            if email and len(password) >= 8:
                repo.upsert_user(email, str(form.get("name") or ""), str(form.get("role") or "sales"),
                                 hash_password(password))
        elif action == "reset":
            email = str(form.get("email") or "").strip().lower()
            password = str(form.get("password") or "")
            user = repo.get_user(email) if email else None
            if user is not None and len(password) >= 8:
                repo.upsert_user(email, user.name, user.role, hash_password(password))
        elif action == "delete":
            repo.delete_user(str(form.get("email") or ""))
    finally:
        try:
            repo.close()
        except Exception:
            pass
    return RedirectResponse(url="/ui/users", status_code=303)


STAGE_AUTODETECT = {
    "proposal": ("proposal", "prospect", "pitch", "present", "quote", "sent"),
    "won": ("won", "closed won", "booked", "won - media"),
    "lost": ("lost", "closed lost", "no sale", "canceled", "cancelled"),
}


def _suggest_stage(stages: list[dict], tokens: tuple[str, ...]) -> str:
    for stage in stages:
        label = str(stage.get("label") or "").strip().lower()
        if any(token in label for token in tokens):
            return str(stage["id"])
    return ""


def _current_stage_map() -> dict[str, str]:
    settings = get_settings()
    return {
        "pipeline": settings.hubspot_deal_pipeline,
        "proposal": settings.hubspot_stage_proposal,
        "won": settings.hubspot_stage_won,
        "lost": settings.hubspot_stage_lost,
    }


def _load_pipelines_threadpool() -> list[dict]:
    """deal_pipelines() opens a real 30 s-timeout socket. The POST handler is
    async (it must `await request.form()`), so a synchronous call here would
    freeze the web container's event loop — which also serves webhooks — for
    the whole round trip. Hand it to Starlette's threadpool instead."""
    from starlette.concurrency import run_in_threadpool

    from app.hubspot.client import HubSpotClient

    return run_in_threadpool(HubSpotClient().deal_pipelines)


def _normalize_pipeline(value: Any) -> str:
    # HubSpot's built-in pipeline id is literally "default"; storing it would
    # make the mapping look "custom" and silence the legacy proposal-stage
    # rescue for a portal that never needed one. Default means "leave blank".
    text = str(value or "").strip()[:100]
    return "" if text.lower() == "default" else text


@router.get("/stages", response_class=HTMLResponse)
def stages_page(request: Request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    pipelines: list[dict] = []
    error = None
    try:
        from app.hubspot.client import HubSpotClient

        pipelines = HubSpotClient().deal_pipelines()
    except Exception as exc:
        error = f"Could not load deal pipelines from HubSpot: {exc}"
    return _page(request, "stages.html", {"pipelines": pipelines, "current": _current_stage_map(), "error": error})


@router.post("/stages")
async def stages_save(request: Request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    form = await request.form()
    action = str(form.get("action") or "")
    settings = get_settings()

    # Both branches call HubSpot; abort-without-writing on failure so a
    # transient error can never persist a blank mapping over a working one.
    try:
        pipelines = await _load_pipelines_threadpool()
    except Exception as exc:
        error = f"Could not load deal pipelines from HubSpot: {exc} — nothing was saved."
        return _page(request, "stages.html", {"pipelines": [], "current": _current_stage_map(), "error": error})

    if action == "autodetect":
        pipeline_id = str(form.get("pipeline") or "").strip()
        if not pipeline_id:
            # The blank choice means the BUILT-in pipeline, not "whatever is
            # first" — a portal with custom pipelines lists those first.
            default_pipe = next((p for p in pipelines if str(p.get("id") or "").lower() == "default"), None)
            pipeline_id = str((default_pipe or (pipelines[0] if pipelines else {})).get("id") or "") if (default_pipe or pipelines) else ""
        stages = next((p.get("stages") or [] for p in pipelines if p.get("id") == pipeline_id), [])
        if not stages:
            error = "Autodetect found no stages on that pipeline — nothing was saved."
            return _page(request, "stages.html", {"pipelines": pipelines, "current": _current_stage_map(), "error": error})
        detected = {
            "hubspot_deal_pipeline": _normalize_pipeline(pipeline_id),
            "hubspot_stage_proposal": _suggest_stage(stages, STAGE_AUTODETECT["proposal"]),
            "hubspot_stage_won": _suggest_stage(stages, STAGE_AUTODETECT["won"]),
            "hubspot_stage_lost": _suggest_stage(stages, STAGE_AUTODETECT["lost"]),
        }
        # Autodetect may only IMPROVE the mapping: a token whose label matches
        # nothing keeps whatever the operator already had, so a partial match
        # can never blank a working three-way map into the broken hybrid.
        proposed: dict[str, str] = {}
        for key, value in detected.items():
            if value or not str(getattr(settings, key, "") or "").strip():
                proposed[key] = value
        persist_settings(proposed)
        return RedirectResponse(url="/ui/stages", status_code=303)

    pipeline = _normalize_pipeline(form.get("pipeline"))
    proposal = str(form.get("stage_proposal") or "").strip()[:100]
    won = str(form.get("stage_won") or "").strip()[:100]
    lost = str(form.get("stage_lost") or "").strip()[:100]
    chosen_pipeline = pipeline or "default"
    valid_stage_ids = {s.get("id") for p in pipelines if str(p.get("id")) == chosen_pipeline for s in (p.get("stages") or [])}
    # A custom pipeline has no built-in proposal/closedwon/closedlost, so a
    # partial map there writes tokens HubSpot rejects. It is a valid choice to
    # map NOTHING (stay fully legacy) or EVERYTHING — not some of it.
    mapped_any = bool(proposal or won or lost)
    if chosen_pipeline != "default" and mapped_any and not (proposal and won and lost):
        error = "Custom pipelines need all three stages mapped (or none). Nothing was saved."
        return _page(request, "stages.html", {"pipelines": pipelines, "current": _current_stage_map(), "error": error})
    # Stage ids must belong to the selected pipeline — an id that leaks in
    # from another pipeline (or stale settings) 400s every write.
    for stage_id in (proposal, won, lost):
        if stage_id and valid_stage_ids and stage_id not in valid_stage_ids:
            error = f"Stage id {stage_id} is not on pipeline {chosen_pipeline}. Nothing was saved."
            return _page(request, "stages.html", {"pipelines": pipelines, "current": _current_stage_map(), "error": error})
    persist_settings(
        {
            "hubspot_deal_pipeline": pipeline,
            "hubspot_stage_proposal": proposal,
            "hubspot_stage_won": won,
            "hubspot_stage_lost": lost,
        }
    )
    return RedirectResponse(url="/ui/stages", status_code=303)


@router.get("/deadletters", response_class=HTMLResponse)
def deadletters_page(request: Request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    repo = Repo()
    try:
        return _page(
            request,
            "deadletters.html",
            {
                "rows": repo.list_dead_letters(("open", "frozen")),
                "resolved_rows": repo.list_dead_letters(("resolved",), limit=8),
                "counts": repo.dead_letter_counts(),
            },
        )
    finally:
        try:
            repo.close()
        except Exception:
            pass


@router.post("/deadletters")
async def deadletters_action(request: Request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    form = await request.form()
    repo = Repo()
    action = str(form.get("action") or "")
    try:
        row_id = int(form.get("row_id") or 0)
    except (TypeError, ValueError):
        row_id = 0
    try:
        if action == "retry" and row_id:
            # Nudges the row to the front of the scheduled reconciliation queue;
            # each retry is a fresh targeted sync, so it uses current settings/logic.
            repo.retry_dead_letter_now(row_id)
        elif action == "resolved" and row_id:
            identity = session_identity(request) or {}
            repo.resolve_dead_letter_by_id(
                row_id, f"marked resolved by {identity.get('email') or 'admin'}"
            )
        elif action == "unfreeze-all":
            repo.unfreeze_dead_letters()
        elif action == "delete" and row_id:
            repo.delete_dead_letter(row_id)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("dead-letter action %s failed", action)
    finally:
        try:
            repo.close()
        except Exception:
            pass
    return RedirectResponse(url="/ui/deadletters", status_code=303)


@router.get("/records", response_class=HTMLResponse)
def records_page(request: Request, q: str = "", entity: str = ""):
    """Record history lookup — "who changed what on deal X, and when?" —
    straight from the per-run item audit already stored by every sync."""
    redirect = _require_login(request)
    if redirect:
        return redirect
    repo = Repo()
    entries: list[dict[str, object]] = []
    try:
        for item, run in repo.search_history(q, entity or None):
            diff = None
            if item.diff_json:
                try:
                    diff = json.loads(item.diff_json)
                except json.JSONDecodeError:
                    diff = None
            entries.append({"item": item, "run": run, "diff": diff})
    finally:
        try:
            repo.close()
        except Exception:
            pass
    return _page(
        request,
        "records.html",
        {"q": q, "entity": entity, "entries": entries if (q or entity) else []},
    )


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
    from app.sync.orchestrator import SyncContext
    from app.sync.worker import enqueue_sync

    result = enqueue_sync(SyncContext(trigger=trigger, whatif=whatif, aquira_id=aquira_id or None))
    run_id = result.get("run_id")
    if run_id:
        return RedirectResponse(url=f"/ui/runs/{run_id}", status_code=303)
    return RedirectResponse(url="/ui", status_code=303)


@router.post("/sync/run")
async def run_sync(request: Request):
    redirect = _require_admin(request)
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
    return _execute_sync(whatif=whatif, trigger="manual", aquira_id=aquira_id)
