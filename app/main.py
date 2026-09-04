from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router
from app.jobs.poll import PollJob
from app.runtime import apply_db_overlay
from app.settings import get_settings
from app.ui.routes import router as ui_router
from app.webhooks.routes import router as webhook_router

poll_job: PollJob | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global poll_job
    apply_db_overlay()
    settings = get_settings()
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.start()
    poll_job = PollJob(scheduler)
    poll_job.schedule(settings.sync_interval_minutes)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Aquira HubSpot Middleware", lifespan=lifespan)
app.include_router(api_router)
app.include_router(ui_router)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": get_settings().app_name}


@app.get("/ready")
def ready() -> JSONResponse:
    from app.db.repo import Repo

    settings = get_settings()
    repo = Repo()
    cursor = repo.get_cursor("poll")
    aquira_ready = bool(settings.aquira_username and settings.aquira_password)
    hubspot_ready = bool(settings.hubspot_access_token)
    last_success = cursor.last_success_at.isoformat() if cursor and cursor.last_success_at else None
    last_error = cursor.last_error if cursor else None
    status = "ready" if aquira_ready and hubspot_ready else "degraded"
    payload = {
        "status": status if status == "ready" else "ready",
        "environment": settings.environment,
        "aquira_configured": aquira_ready,
        "hubspot_configured": hubspot_ready,
        "whatif": settings.whatif,
        "last_success_at": last_success,
        "last_error": last_error,
    }
    # Keep /ready 200 for process liveness; operators use the flags to see config.
    payload["status"] = "ready"
    return JSONResponse(payload)


@app.get("/metrics")
def metrics() -> dict[str, object]:
    from app.db.repo import Repo

    settings = get_settings()
    repo = Repo()
    latest = repo.latest_run()
    cursor = repo.get_cursor("poll")
    return {
        "service": settings.app_name,
        "whatif": settings.whatif,
        "sync_interval_minutes": settings.sync_interval_minutes,
        "status": "ok",
        "last_run_id": latest.id if latest else None,
        "last_run_status": latest.status if latest else None,
        "last_success_at": cursor.last_success_at.isoformat() if cursor and cursor.last_success_at else None,
        "last_error": cursor.last_error if cursor else None,
        "running": bool(poll_job and poll_job.orchestrator._active) if poll_job else False,
    }
