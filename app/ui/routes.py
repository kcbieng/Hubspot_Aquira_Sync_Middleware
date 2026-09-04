import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db.models import SyncRun, SyncRunItem
from app.db.repo import Repo
from app.settings import get_settings
from app.sync.orchestrator import SyncContext, SyncOrchestrator

router = APIRouter(prefix="/ui", tags=["ui"])
templates = Jinja2Templates(directory="app/ui/templates")


def _latest_sync_output() -> dict[str, object]:
    repo = Repo()
    run = repo.session.execute(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)).scalar_one_or_none()
    items: list[dict[str, object]] = []
    if run is not None:
        rows = repo.session.execute(
            select(SyncRunItem)
            .where(SyncRunItem.run_id == run.id)
            .order_by(SyncRunItem.id.desc())
            .limit(10)
        ).scalars().all()
        for row in rows:
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
    return {"run": run, "items": items}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "settings": get_settings(), "error": None})


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    settings = get_settings()
    if username == settings.ui_username and password == settings.ui_password:
        response = RedirectResponse(url="/ui", status_code=303)
        response.set_cookie("middleware_session", "authenticated", httponly=True, samesite="lax")
        return response
    return templates.TemplateResponse("login.html", {"request": request, "settings": settings, "error": "Invalid credentials"})


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request):
    if not request.cookies.get("middleware_session"):
        return RedirectResponse(url="/ui/login", status_code=303)
    settings = get_settings()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "settings": settings,
            "mode_label": "PLAN ONLY — no writes" if settings.whatif else "LIVE WRITES",
            "status": "ready",
            "recent_sync_output": _latest_sync_output(),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    if not request.cookies.get("middleware_session"):
        return RedirectResponse(url="/ui/login", status_code=303)
    return templates.TemplateResponse("settings.html", {"request": request, "settings": get_settings(), "error": None})


@router.post("/settings")
async def update_settings(request: Request):
    if not request.cookies.get("middleware_session"):
        return RedirectResponse(url="/ui/login", status_code=303)

    form = await request.form()
    settings = get_settings()
    settings.whatif = str(form.get("whatif", "false")).lower() in {"1", "true", "on", "yes"}
    try:
        settings.sync_interval_minutes = int(form.get("sync_interval_minutes", settings.sync_interval_minutes))
    except ValueError:
        settings.sync_interval_minutes = 30

    response = RedirectResponse(url="/ui", status_code=303)
    return response


@router.get("/owners", response_class=HTMLResponse)
def owners_page(request: Request):
    if not request.cookies.get("middleware_session"):
        return RedirectResponse(url="/ui/login", status_code=303)
    return templates.TemplateResponse("owners.html", {"request": request, "settings": get_settings(), "rows": []})


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    if not request.cookies.get("middleware_session"):
        return RedirectResponse(url="/ui/login", status_code=303)
    repo = Repo()
    rows = repo.session.execute(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(50)
    ).scalars().all()
    return templates.TemplateResponse("runs.html", {"request": request, "settings": get_settings(), "runs": rows})


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(request: Request, run_id: int):
    if not request.cookies.get("middleware_session"):
        return RedirectResponse(url="/ui/login", status_code=303)
    repo = Repo()
    run = repo.session.get(SyncRun, run_id)
    items = []
    if run is not None:
        items = repo.session.execute(
            select(SyncRunItem).where(SyncRunItem.run_id == run_id).order_by(SyncRunItem.id.desc())
        ).scalars().all()
    return templates.TemplateResponse("run_detail.html", {"request": request, "settings": get_settings(), "run": run, "items": items})


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    if not request.cookies.get("middleware_session"):
        return RedirectResponse(url="/ui/login", status_code=303)
    repo = Repo()
    rows = repo.session.execute(
        select(__import__("app.db.models", fromlist=["JobEvent"]).JobEvent).order_by(__import__("app.db.models", fromlist=["JobEvent"]).JobEvent.ts.desc()).limit(200)
    ).scalars().all()
    return templates.TemplateResponse("logs.html", {"request": request, "settings": get_settings(), "events": rows})


@router.post("/sync/run")
async def run_sync(request: Request):
    if not request.cookies.get("middleware_session"):
        return RedirectResponse(url="/ui/login", status_code=303)
    settings = get_settings()
    SyncOrchestrator().run(
        SyncContext(trigger="manual", whatif=settings.whatif, entities=["companies", "contacts", "deals"]),
        repo=Repo(),
    )
    response = RedirectResponse(url="/ui", status_code=303)
    return response
