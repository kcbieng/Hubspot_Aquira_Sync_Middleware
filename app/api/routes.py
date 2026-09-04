from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from app.aquira.client import AquiraSessionClient, test_aquira_connection
from app.db.models import OwnerMap, TeamMap
from app.db.repo import Repo
from app.hubspot.client import HubSpotClient
from app.mapping.owners import suggest_owner_map
from app.mapping.teams import collect_team_keys, suggest_team_map, team_attribute_names
from app.runtime import mask_secret, persist_settings
from app.session import clear_session, set_session
from app.settings import get_settings
from app.sync.orchestrator import SyncContext
from app.sync.worker import enqueue_sync, is_busy, queue_size, wait_for_run

router = APIRouter(prefix="/api", tags=["api"])


def _public_settings() -> dict[str, object]:
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "whatif": settings.whatif,
        "sync_interval_minutes": settings.sync_interval_minutes,
        "sync_calls": settings.sync_calls,
        "sync_create_aquira_client": settings.sync_create_aquira_client,
        "ui_username": settings.ui_username,
        "aquira_base_url": settings.aquira_base_url,
        "aquira_username": settings.aquira_username,
        "aquira_password": mask_secret(settings.aquira_password),
        "hubspot_access_token": mask_secret(settings.hubspot_access_token),
        "hubspot_client_secret": mask_secret(settings.hubspot_client_secret),
        "bootstrap_hubspot": settings.bootstrap_hubspot,
        "aquira_configured": bool(settings.aquira_username and settings.aquira_password),
        "hubspot_configured": bool(settings.hubspot_access_token),
    }


@router.post("/login")
def login(payload: dict[str, str]) -> JSONResponse:
    settings = get_settings()
    username = payload.get("username", "")
    password = payload.get("password", "")
    if username != settings.ui_username or password != settings.ui_password:
        raise HTTPException(status_code=401, detail="invalid credentials")
    response = JSONResponse({"ok": True, "username": username})
    set_session(response)
    return response


@router.post("/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    clear_session(response)
    return response


@router.get("/settings")
def get_settings_stub() -> dict[str, object]:
    return _public_settings()


@router.post("/settings/test/aquira")
def test_aquira_settings() -> dict[str, object]:
    return test_aquira_connection()


@router.post("/settings/test/hubspot")
def test_hubspot_settings() -> dict[str, object]:
    client = HubSpotClient()
    if not client.access_token:
        return {"status": "error", "mode": "live", "message": "HubSpot token missing", "portal": "unconfigured"}
    return client.test_connection()


@router.put("/settings")
def update_settings(payload: dict[str, object]) -> dict[str, object]:
    persist_settings(payload)
    return _public_settings()


@router.get("/sync/status")
def sync_status_stub() -> dict[str, object]:
    repo = Repo()
    latest = repo.latest_run()
    current_setting = get_settings().whatif
    effective_whatif = latest.whatif if latest is not None else current_setting
    cursor = repo.get_cursor("poll")
    return {
        "status": latest.status if latest else "ok",
        "message": "sync status ready",
        "whatif": effective_whatif,
        "running": is_busy(),
        "queue": queue_size(),
        "last_started": latest.started_at.isoformat() if latest and latest.started_at else None,
        "last_finished": latest.finished_at.isoformat() if latest and latest.finished_at else None,
        "last_success_at": cursor.last_success_at.isoformat() if cursor and cursor.last_success_at else None,
        "last_error": cursor.last_error if cursor else (latest.error if latest else None),
    }


def _run_sync_now(payload: dict[str, object] | None = None, trigger: str = "manual") -> dict[str, object]:
    settings = get_settings()
    body = payload or {}
    whatif = bool(body.get("whatif", settings.whatif))
    entities = body.get("entities")
    aquira_id = body.get("aquira_id") or body.get("aquiraId")
    result = enqueue_sync(
        SyncContext(
            trigger=str(body.get("trigger") or trigger),
            whatif=whatif,
            entities=list(entities) if entities else None,
            aquira_id=str(aquira_id) if aquira_id else None,
        )
    )
    if body.get("wait"):
        finished = wait_for_run(int(result["run_id"]), timeout=float(body.get("wait_timeout") or 60))
        if finished:
            return finished
    return result


@router.post("/sync/run")
def run_sync(payload: dict[str, object] | None = None) -> dict[str, object]:
    return _run_sync_now(payload, trigger="manual")


@router.post("/sync/client/{aquira_id}")
def run_sync_client(aquira_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    body = dict(payload or {})
    body["aquira_id"] = aquira_id
    body.setdefault("entities", ["companies", "contacts", "writeback"])
    return _run_sync_now(body, trigger="single")


@router.post("/sync/contract/{aquira_id}")
def run_sync_contract(aquira_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    body = dict(payload or {})
    body["aquira_id"] = aquira_id
    body.setdefault("entities", ["deals", "revenue"])
    return _run_sync_now(body, trigger="single")


@router.get("/sync/runs")
def sync_runs() -> list[dict[str, object]]:
    repo = Repo()
    runs = repo.list_runs(50)
    return [
        {
            "id": run.id,
            "trigger": run.trigger,
            "whatif": run.whatif,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "summary_json": run.summary_json,
            "error": run.error,
        }
        for run in runs
    ]


@router.get("/sync/runs/{run_id}")
def sync_run_detail(run_id: int) -> dict[str, object]:
    repo = Repo()
    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    items = repo.list_run_items(run_id)
    return {
        "id": run.id,
        "trigger": run.trigger,
        "whatif": run.whatif,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "summary_json": run.summary_json,
        "error": run.error,
        "items": [
            {
                "id": item.id,
                "entity_type": item.entity_type,
                "aquira_id": item.aquira_id,
                "hubspot_id": item.hubspot_id,
                "action": item.action,
                "diff_json": item.diff_json,
                "error": item.error,
            }
            for item in items
        ],
    }


@router.get("/owners/suggest")
def owner_suggest() -> list[dict[str, object]]:
    aquira_reps = aquira_owners()
    hubspot_reps = hubspot_owners()
    return suggest_owner_map(aquira_reps, hubspot_reps)


@router.put("/owners/map")
def update_owner_map(payload: list[dict[str, object]]) -> list[dict[str, object]]:
    repo = Repo()
    for suggestion in payload:
        repo.session.merge(
            OwnerMap(
                aquira_user_id=str(suggestion.get("aquira_user_id")),
                aquira_name=suggestion.get("aquira_name"),
                aquira_email=suggestion.get("aquira_email"),
                hubspot_owner_id=suggestion.get("hubspot_owner_id"),
                hubspot_name=suggestion.get("hubspot_name"),
                hubspot_email=suggestion.get("hubspot_email"),
                enabled=bool(suggestion.get("enabled")),
                suggested=bool(suggestion.get("suggested")),
                updated_at=datetime.utcnow(),
            )
        )
    repo.session.commit()
    return payload


@router.put("/settings/whatif")
def whatif_toggle(payload: dict[str, bool]) -> dict[str, bool]:
    enabled = payload.get("enabled", False)
    persist_settings({"whatif": enabled})
    return {"enabled": get_settings().whatif}


@router.get("/owners/aquira")
def aquira_owners() -> list[dict[str, object]]:
    client = AquiraSessionClient()
    try:
        if not getattr(client, "logged_in", True):
            client.login()
        reps = client.load_sales_reps()
    except Exception:
        reps = []
    finally:
        try:
            client.close()
        except Exception:
            pass
    return [
        {
            "id": rep.get("id") or rep.get("ID"),
            "name": rep.get("name") or rep.get("Name"),
            "email": rep.get("email") or rep.get("Email"),
        }
        for rep in reps
    ]



@router.get("/owners/hubspot")
def hubspot_owners() -> list[dict[str, object]]:
    client = HubSpotClient()
    try:
        lister = getattr(client, "list_sales_users", None)
        if callable(lister):
            return lister()
        payload = client.get_owners()
    except Exception:
        return []
    rows: list[dict[str, object]] = []
    for owner in payload.get("results", []):
        first = owner.get("firstName") or ""
        last = owner.get("lastName") or ""
        name = f"{first} {last}".strip() or owner.get("name") or owner.get("email") or ""
        rows.append(
            {
                "owner_id": str(owner.get("id") or owner.get("ownerId") or ""),
                "name": name,
                "email": owner.get("email") or "",
                "kind": "user",
                "role": "",
                "super_admin": False,
            }
        )
    return rows


@router.get("/owners/map")
def owner_map() -> list[dict[str, object]]:
    aquira_reps = aquira_owners()
    hubspot_reps = hubspot_owners()
    suggestions = suggest_owner_map(aquira_reps, hubspot_reps)
    repo = Repo()
    try:
        for suggestion in suggestions:
            aquira_id = str(suggestion.get("aquira_user_id"))
            existing = repo.session.get(OwnerMap, aquira_id)
            if existing is not None and not existing.suggested:
                existing.aquira_name = suggestion.get("aquira_name") or existing.aquira_name
                existing.aquira_email = suggestion.get("aquira_email") or existing.aquira_email
                repo.session.add(existing)
                continue
            repo.session.merge(
                OwnerMap(
                    aquira_user_id=aquira_id,
                    aquira_name=suggestion.get("aquira_name"),
                    aquira_email=suggestion.get("aquira_email"),
                    hubspot_owner_id=suggestion.get("hubspot_owner_id"),
                    hubspot_name=suggestion.get("hubspot_name"),
                    hubspot_email=suggestion.get("hubspot_email"),
                    enabled=bool(suggestion.get("enabled")),
                    suggested=bool(suggestion.get("suggested")),
                    updated_at=datetime.utcnow(),
                )
            )
        repo.session.commit()
    finally:
        repo.close()
    return suggestions


@router.get("/teams/hubspot")
def hubspot_teams() -> list[dict[str, object]]:
    client = HubSpotClient()
    try:
        return client.list_teams()
    except Exception:
        return []


@router.get("/teams/aquira")
def aquira_team_keys() -> list[dict[str, object]]:
    client = AquiraSessionClient()
    try:
        client.login()
        catalog = client.load_catalog()
    except Exception:
        catalog = {"clients": [], "contacts": [], "contracts": []}
    finally:
        try:
            client.close()
        except Exception:
            pass
    return collect_team_keys(catalog, team_attribute_names(get_settings().aquira_team_attribute))


@router.get("/teams/suggest")
def team_suggest() -> list[dict[str, object]]:
    return suggest_team_map(aquira_team_keys(), hubspot_teams())


@router.get("/teams/map")
def team_map() -> list[dict[str, object]]:
    suggestions = suggest_team_map(aquira_team_keys(), hubspot_teams())
    repo = Repo()
    try:
        for suggestion in suggestions:
            aquira_key = str(suggestion.get("aquira_key") or "")
            if not aquira_key:
                continue
            existing = repo.session.get(TeamMap, aquira_key)
            if existing is not None and not existing.suggested:
                existing.aquira_label = suggestion.get("aquira_label") or existing.aquira_label
                existing.source = suggestion.get("source") or existing.source
                repo.session.add(existing)
                continue
            repo.session.merge(
                TeamMap(
                    aquira_key=aquira_key,
                    aquira_label=suggestion.get("aquira_label"),
                    source=suggestion.get("source"),
                    hubspot_team_id=suggestion.get("hubspot_team_id"),
                    hubspot_team_name=suggestion.get("hubspot_team_name"),
                    enabled=bool(suggestion.get("enabled")),
                    suggested=bool(suggestion.get("suggested")),
                    updated_at=datetime.utcnow(),
                )
            )
        repo.session.commit()
    finally:
        repo.close()
    return suggestions


@router.put("/teams/map")
def update_team_map(payload: list[dict[str, object]]) -> list[dict[str, object]]:
    repo = Repo()
    for suggestion in payload:
        aquira_key = str(suggestion.get("aquira_key") or "")
        if not aquira_key:
            continue
        repo.session.merge(
            TeamMap(
                aquira_key=aquira_key,
                aquira_label=suggestion.get("aquira_label"),
                source=suggestion.get("source"),
                hubspot_team_id=suggestion.get("hubspot_team_id"),
                hubspot_team_name=suggestion.get("hubspot_team_name"),
                enabled=bool(suggestion.get("enabled")),
                suggested=bool(suggestion.get("suggested")),
                updated_at=datetime.utcnow(),
            )
        )
    repo.session.commit()
    return payload
