from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse

from app.settings import get_settings

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
def sync_status_stub() -> dict[str, str]:
    return {"status": "ok", "message": "sync status endpoint ready", "whatif": str(get_settings().whatif).lower()}


@router.put("/settings/whatif")
def whatif_toggle(payload: dict[str, bool]) -> dict[str, bool]:
    enabled = payload.get("enabled", False)
    settings = get_settings()
    settings.whatif = enabled
    return {"enabled": settings.whatif}


@router.get("/owners/aquira")
def aquira_owners() -> list[dict[str, str]]:
    return []


@router.get("/owners/hubspot")
def hubspot_owners() -> list[dict[str, str]]:
    return []


@router.get("/owners/map")
def owner_map() -> list[dict[str, str]]:
    return []
