from contextlib import asynccontextmanager
import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import _run_sync_now, router as api_router, run_sync_client, run_sync_contract, sync_status_stub
from app.jobs.poll import PollJob, set_active_job
from app.runtime import apply_db_overlay
from app.session import is_logged_in
from app.settings import get_settings
from app.ui.routes import router as ui_router
from app.version import REVISION
from app.webhooks.routes import router as webhook_router

poll_job: PollJob | None = None
OPEN_API_PATHS = {"/api/login", "/api/logout"}


def _configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, str(settings.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if settings.environment == "production" and settings.ui_password in {"admin", "password", ""}:
        logging.getLogger("app").warning("UI_PASSWORD is weak; set a strong password in the stack environment.")
    if not settings.settings_fernet_key:
        logging.getLogger("app").warning("SETTINGS_FERNET_KEY is empty; generate one before storing secrets from the UI.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global poll_job
    _configure_logging()
    apply_db_overlay()
    settings = get_settings()
    from app.sync.worker import can_execute_jobs, start_worker

    role = str(settings.hubquira_role or "web").strip().lower()
    logging.getLogger("app").info("HubQuira HTTP process role=%s execute_jobs=%s", role, can_execute_jobs())
    if can_execute_jobs():
        start_worker()
    scheduler = None
    if can_execute_jobs():
        scheduler = BackgroundScheduler(timezone=settings.timezone)
        scheduler.start()
        poll_job = PollJob(scheduler)
        poll_job.schedule(settings.sync_interval_minutes)
        set_active_job(poll_job)
    try:
        yield
    finally:
        set_active_job(None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(title="HubQuira", lifespan=lifespan)
STATIC_DIR = Path(__file__).resolve().parent / "ui" / "static"
app.include_router(api_router)
app.include_router(ui_router)
app.include_router(webhook_router)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/favicon.ico")
def favicon():
    icon = STATIC_DIR / "favicon.ico"
    if icon.exists():
        return FileResponse(icon, media_type="image/x-icon")
    png = STATIC_DIR / "hubquira-logo.png"
    return FileResponse(png, media_type="image/png")


@app.middleware("http")
async def protect_operator_api(request: Request, call_next):
    settings = get_settings()
    if settings.environment == "production":
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/sync/"):
            if path not in OPEN_API_PATHS and not is_logged_in(request):
                return JSONResponse({"detail": "login required"}, status_code=401)
    return await call_next(request)


@app.get("/")
def root():
    return RedirectResponse(url="/ui", status_code=302)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": get_settings().app_name, "revision": REVISION}


@app.get("/ready")
def ready() -> JSONResponse:
    from app.db.repo import Repo

    settings = get_settings()
    repo = Repo()
    try:
        cursor = repo.get_cursor("poll")
        aquira_ready = bool(settings.aquira_username and settings.aquira_password)
        hubspot_ready = bool(settings.hubspot_access_token)
        last_success = cursor.last_success_at.isoformat() if cursor and cursor.last_success_at else None
        last_error = cursor.last_error if cursor else None
        payload = {
            "status": "ready",
            "environment": settings.environment,
            "aquira_configured": aquira_ready,
            "hubspot_configured": hubspot_ready,
            "whatif": settings.whatif,
            "last_success_at": last_success,
            "last_error": last_error,
            "configured": aquira_ready and hubspot_ready,
        }
        return JSONResponse(payload)
    finally:
        repo.close()


@app.get("/metrics")
def metrics() -> dict[str, object]:
    from app.db.repo import Repo

    settings = get_settings()
    repo = Repo()
    try:
        latest = repo.latest_run()
        cursor = repo.get_cursor("poll")
        from app.sync.worker import is_busy, queue_size

        return {
            "service": settings.app_name,
            "whatif": settings.whatif,
            "sync_interval_minutes": settings.sync_interval_minutes,
            "status": "ok",
            "last_run_id": latest.id if latest else None,
            "last_run_status": latest.status if latest else None,
            "last_success_at": cursor.last_success_at.isoformat() if cursor and cursor.last_success_at else None,
            "last_error": cursor.last_error if cursor else None,
            "running": is_busy(),
            "queue": queue_size(),
        }
    finally:
        repo.close()


@app.post("/sync/run")
def sync_run_alias(payload: dict[str, object] | None = None) -> dict[str, object]:
    return _run_sync_now(payload, trigger="manual")


@app.post("/sync/client/{aquira_id}")
def sync_client_alias(aquira_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return run_sync_client(aquira_id, payload)


@app.post("/sync/contract/{aquira_id}")
def sync_contract_alias(aquira_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return run_sync_contract(aquira_id, payload)


@app.get("/sync/status")
def sync_status_alias() -> dict[str, object]:
    return sync_status_stub()
