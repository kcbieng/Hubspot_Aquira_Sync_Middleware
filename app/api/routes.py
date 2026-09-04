from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse

from app.db.models import OwnerMap
from app.db.repo import Repo
from app.hubspot.client import HubSpotClient
from app.mapping.owners import suggest_owner_map
from app.settings import get_settings
from app.aquira.client import AquiraSessionClient
from app.sync.orchestrator import SyncContext, SyncOrchestrator

router = APIRouter(prefix="/api", tags=["api"])


def _mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"••••{value[-4:]}"


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
def logout() -> dict[str, str]:
    response = JSONResponse({"ok": True})
    response.delete_cookie("middleware_session")
    return response


@router.get("/settings")
def get_settings_stub() -> dict[str, object]:
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "whatif": settings.whatif,
        "sync_interval_minutes": settings.sync_interval_minutes,
        "sync_calls": settings.sync_calls,
        "ui_username": settings.ui_username,
        "aquira_base_url": settings.aquira_base_url,
        "hubspot_access_token": _mask_secret(settings.hubspot_access_token),
        "hubspot_client_secret": _mask_secret(settings.hubspot_client_secret),
        "bootstrap_hubspot": settings.bootstrap_hubspot,
    }


@router.api_route("/settings/test/aquira", methods=["GET", "POST"])
def test_aquira_settings() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok" if settings.aquira_base_url else "error",
        "message": "Aquira config present" if settings.aquira_base_url else "Aquira base URL missing",
        "aquira_base_url": settings.aquira_base_url,
    }


@router.api_route("/settings/test/hubspot", methods=["GET", "POST"])
def test_hubspot_settings() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok" if settings.hubspot_access_token else "error",
        "message": "HubSpot token present" if settings.hubspot_access_token else "HubSpot token missing",
        "portal": "configured" if settings.hubspot_access_token else "unconfigured",
    }


@router.put("/settings")
def update_settings(payload: dict[str, object]) -> dict[str, object]:
    settings = get_settings()
    if "whatif" in payload:
        settings.whatif = bool(payload["whatif"])
    if "sync_interval_minutes" in payload:
        settings.sync_interval_minutes = int(payload["sync_interval_minutes"])
    if "sync_calls" in payload:
        settings.sync_calls = bool(payload["sync_calls"])
    if "bootstrap_hubspot" in payload:
        settings.bootstrap_hubspot = bool(payload["bootstrap_hubspot"])
    return get_settings_stub()


@router.get("/sync/status")
def sync_status_stub() -> dict[str, object]:
    repo = Repo()
    latest = repo.session.execute(
        __import__("sqlalchemy").select(__import__("app.db.models", fromlist=["SyncRun"]).SyncRun).order_by(__import__("app.db.models", fromlist=["SyncRun"]).SyncRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    current_setting = get_settings().whatif
    effective_whatif = latest.whatif if latest is not None else current_setting
    return {
        "status": latest.status if latest else "ok",
        "message": "sync status ready",
        "whatif": effective_whatif,
        "last_started": latest.started_at.isoformat() if latest and latest.started_at else None,
        "last_finished": latest.finished_at.isoformat() if latest and latest.finished_at else None,
    }


@router.post("/sync/run")
def run_sync(payload: dict[str, object] | None = None) -> dict[str, object]:
    settings = get_settings()
    whatif = bool((payload or {}).get("whatif", settings.whatif))
    settings.whatif = whatif

    entities = payload.get("entities") if payload else None
    if entities is None:
        entities = ["companies", "contacts", "deals"]
    entity_list = [str(entity) for entity in entities]

    result = SyncOrchestrator().run(
        SyncContext(trigger="manual", whatif=whatif, entities=entity_list),
        repo=Repo(),
    )
    return {
        "status": result["status"],
        "trigger": "manual",
        "whatif": whatif,
        "entities": entity_list,
        "run_id": result["run_id"],
    }


@router.get("/sync/runs")
def sync_runs() -> list[dict[str, object]]:
    repo = Repo()
    runs = repo.session.execute(
        __import__("sqlalchemy").select(__import__("app.db.models", fromlist=["SyncRun"]).SyncRun).order_by(__import__("app.db.models", fromlist=["SyncRun"]).SyncRun.started_at.desc()).limit(50)
    ).scalars().all()
    return [
        {
            "id": run.id,
            "trigger": run.trigger,
            "whatif": run.whatif,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
        for run in runs
    ]


@router.get("/sync/runs/{run_id}")
def sync_run_detail(run_id: int) -> dict[str, object]:
    repo = Repo()
    run = repo.session.get(__import__("app.db.models", fromlist=["SyncRun"]).SyncRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    items = repo.session.execute(
        __import__("sqlalchemy").select(__import__("app.db.models", fromlist=["SyncRunItem"]).SyncRunItem)
        .where(__import__("app.db.models", fromlist=["SyncRunItem"]).SyncRunItem.run_id == run_id)
        .order_by(__import__("app.db.models", fromlist=["SyncRunItem"]).SyncRunItem.id.desc())
    ).scalars().all()
    return {
        "id": run.id,
        "trigger": run.trigger,
        "whatif": run.whatif,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
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
            )
        )
    repo.session.commit()
    return payload


@router.put("/settings/whatif")
def whatif_toggle(payload: dict[str, bool]) -> dict[str, bool]:
    enabled = payload.get("enabled", False)
    settings = get_settings()
    settings.whatif = enabled
    return {"enabled": settings.whatif}


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
                "owner_id": str(owner.get("ownerId")),
                "name": owner.get("firstName") + " " + owner.get("lastName") if owner.get("firstName") or owner.get("lastName") else owner.get("name"),
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
            )
        )
    repo.session.commit()
    return suggestions
