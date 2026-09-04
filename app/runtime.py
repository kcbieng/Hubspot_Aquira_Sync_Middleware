from __future__ import annotations

import base64
import hashlib
from typing import Any

from app.settings import Settings, get_settings

SECRET_KEYS = {
    "aquira_password",
    "hubspot_access_token",
    "hubspot_client_secret",
    "ui_password",
}

BOOL_KEYS = {
    "whatif",
    "sync_calls",
    "sync_create_aquira_client",
    "bootstrap_hubspot",
}

INT_KEYS = {"sync_interval_minutes"}


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover
        return None
    raw = (get_settings().settings_fernet_key or "dev-change-me").encode("utf-8")
    try:
        return Fernet(raw)
    except Exception:
        digest = hashlib.sha256(raw).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(value: str) -> str:
    fernet = _fernet()
    if fernet is None:
        return value
    return fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_value(value: str | None) -> str | None:
    if value is None:
        return None
    fernet = _fernet()
    if fernet is None:
        return value
    try:
        return fernet.decrypt(value.encode("utf-8")).decode("utf-8")
    except Exception:
        return value


def coerce_setting(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "on", "yes"}
    if key in INT_KEYS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return str(value)


def persist_settings(values: dict[str, Any]) -> Settings:
    from app.db.repo import Repo

    settings = get_settings()
    repo = Repo()
    for key, raw in values.items():
        if not hasattr(settings, key) or raw is None:
            continue
        if key in SECRET_KEYS and isinstance(raw, str) and ("••••" in raw or raw == ""):
            continue
        coerced = coerce_setting(key, raw)
        if coerced is None:
            continue
        setattr(settings, key, coerced)
        stored = str(coerced)
        if key in SECRET_KEYS:
            stored = encrypt_value(stored)
        repo.set_setting(key, stored)
    if "sync_interval_minutes" in values:
        try:
            from app.jobs.poll import reschedule_active

            reschedule_active(int(settings.sync_interval_minutes))
        except Exception:
            pass
    return settings



def apply_db_overlay() -> Settings:
    from app.db.repo import Repo

    settings = get_settings()
    try:
        repo = Repo()
        rows = repo.all_settings()
    except Exception:
        return settings
    for key, stored in rows.items():
        if not hasattr(settings, key) or stored is None:
            continue
        plain = decrypt_value(stored) if key in SECRET_KEYS else stored
        coerced = coerce_setting(key, plain)
        if coerced is None:
            continue
        setattr(settings, key, coerced)
    return settings


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"••••{value[-4:]}"
