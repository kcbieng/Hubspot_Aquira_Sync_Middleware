from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from app.aquira.client import AquiraSessionClient, test_aquira_connection
from app.db.models import OwnerMap
from app.db.repo import Repo
from app.hubspot.client import HubSpotClient
from app.mapping.owners import suggest_owner_map
from app.runtime import mask_secret, persist_settings
from app.settings import get_settings
from app.sync.orchestrator import SyncContext, SyncOrchestrator
from app.sync.whatif import SyncInProgress

router = APIRouter(prefix="/api", tags=["api"])
_orchestrator = SyncOrchestrator()


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
    response.set_cookie("middleware_session", "authenticated", httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("middleware_session")
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
        "running": _orchestrator._active,
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
    try:
        result = _orchestrator.run(
            SyncContext(
                trigger=str(body.get("trigger") or trigger),
                whatif=whatif,
                entities=list(entities) if entities else None,
                aquira_id=str(aquira_id) if aquira_id else None,
            ),
            repo=Repo(),
        )
        return result
    except SyncInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
def hubspot_owners() -> list[dict[str, str]]:
    client = HubSpotClient()
    try:
        payload = client.get_owners()
    except Exception:
        payload = {"results": []}
    results = []
    for owner in payload.get("results", []):
        results.append(
            {
                "owner_id": str(owner.get("id") or owner.get("ownerId") or ""),
                "name": (
                    owner.get("firstName") + " " + owner.get("lastName")
                    if owner.get("firstName") or owner.get("lastName")
                    else owner.get("name")
                ),
                "email": owner.get("email"),
            }
        )
    return results


@router.get("/owners/map")
def owner_map() -> list[dict[str, object]]:
    aquira_reps = aquira_owners()
    hubspot_reps = hubspot_owners()
    suggestions = suggest_owner_map(aquira_reps, hubspot_reps)
    repo = Repo()
    for suggestion in suggestions:
        repo.session.merge(
            OwnerMap(
                aquira_user_id=str(suggestion["aquira_user_id"]),
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
    return suggestions
