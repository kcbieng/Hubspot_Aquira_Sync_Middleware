from __future__ import annotations

import hashlib

from fastapi.responses import Response
from starlette.requests import Request

from app.auth import parse_user_session_token
from app.settings import get_settings

COOKIE_NAME = "middleware_session"


def session_token() -> str:
    settings = get_settings()
    material = f"{settings.ui_username}\0{settings.ui_password}\0{settings.settings_fernet_key or 'dev'}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def cookie_params() -> dict[str, object]:
    settings = get_settings()
    secure = str(settings.public_base_url or "").startswith("https://")
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "max_age": 60 * 60 * 12,
        "path": "/",
    }


def set_session(response: Response) -> None:
    response.set_cookie(COOKIE_NAME, session_token(), **cookie_params())


def set_user_session(response: Response, email: str, role: str) -> None:
    from app.auth import user_session_token

    response.set_cookie(COOKIE_NAME, user_session_token(email, role), **cookie_params())


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def session_identity(request: Request) -> dict[str, str] | None:
    """None = anonymous. The env-configured credential is always admin."""
    settings = get_settings()
    token = request.cookies.get(COOKIE_NAME) or ""
    if token and hmac_equal(token, session_token()):
        return {"email": settings.ui_username, "role": "admin", "source": "config"}
    parsed = parse_user_session_token(token)
    if parsed:
        return {"email": parsed[0], "role": parsed[1], "source": "user"}
    return None


def hmac_equal(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(str(a), str(b))


def is_logged_in(request: Request) -> bool:
    return session_identity(request) is not None


def current_role(request: Request) -> str:
    identity = session_identity(request)
    return str((identity or {}).get("role") or "")
