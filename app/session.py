from __future__ import annotations

import hashlib

from fastapi.responses import Response
from starlette.requests import Request

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


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def is_logged_in(request: Request) -> bool:
    return request.cookies.get(COOKIE_NAME) == session_token()
