from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.api.routes import router as api_router
from app.jobs.poll import PollJob
from app.settings import get_settings
from app.ui.routes import router as ui_router
from app.webhooks.routes import router as webhook_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    scheduler = BackgroundScheduler()
    scheduler.start()
    PollJob(scheduler).schedule(settings.sync_interval_minutes)
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
def ready() -> dict[str, str]:
    return {"status": "ready", "environment": get_settings().environment}


@app.get("/metrics")
def metrics() -> dict[str, object]:
    return {
        "service": get_settings().app_name,
        "whatif": get_settings().whatif,
        "sync_interval_minutes": get_settings().sync_interval_minutes,
        "status": "ok",
    }
