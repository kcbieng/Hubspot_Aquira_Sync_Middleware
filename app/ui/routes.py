from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.settings import get_settings

router = APIRouter(prefix="/ui", tags=["ui"])
templates = Jinja2Templates(directory="app/ui/templates")


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
        },
    )
