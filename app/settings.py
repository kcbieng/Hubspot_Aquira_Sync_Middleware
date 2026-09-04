from __future__ import annotations

import base64
import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _fernet_key_bytes(key: str | None) -> bytes:
    raw = (key or "dev-change-me").strip()
    if not raw:
        raw = "dev-change-me"
    if len(raw) >= 32:
        padded = raw[:32]
    else:
        padded = raw.ljust(32, "0")
    return base64.urlsafe_b64encode(padded.encode("utf-8"))


def encrypt_secret(value: str | None, key: str | None = None) -> str:
    if value in (None, ""):
        return ""
    try:
        from cryptography.fernet import Fernet

        fernet = Fernet(_fernet_key_bytes(key))
        return fernet.encrypt(value.encode("utf-8")).decode("utf-8")
    except Exception:
        encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("utf-8")
        return f"enc:{encoded}"


def decrypt_secret(value: str | None, key: str | None = None) -> str | None:
    if value in (None, ""):
        return None
    try:
        from cryptography.fernet import Fernet

        if value.startswith("enc:"):
            return base64.urlsafe_b64decode(value[4:]).decode("utf-8")
        fernet = Fernet(_fernet_key_bytes(key))
        return fernet.decrypt(value.encode("utf-8")).decode("utf-8")
    except Exception:
        if value.startswith("enc:"):
            try:
                return base64.urlsafe_b64decode(value[4:]).decode("utf-8")
            except Exception:
                return value
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        protected_namespaces=("model_",),
    )

    app_name: str = "aquira_hubspot_middleware"
    environment: str = "development"
    log_level: str = "INFO"
    timezone: str = "America/Chicago"

    aquira_base_url: str = "https://aquira2go.kcbieng.org/Aquira_WebAPI"
    aquira_username: str = ""
    aquira_password: str = ""
    hubspot_access_token: str = ""
    hubspot_client_secret: str = "dev-secret"
    database_url: str = "sqlite:///./app.db"
    sync_interval_minutes: int = 30
    whatif: bool = False
    sync_calls: bool = False
    sync_create_aquira_client: bool = True
    settings_fernet_key: str = "dev-change-me"
    ui_username: str = "admin"
    ui_password: str = "admin"
    bootstrap_hubspot: bool = True

    @property
    def effective_database_url(self) -> str:
        url = (self.database_url or "sqlite:///./app.db").strip()
        if url.startswith("postgresql"):
            try:
                import psycopg2  # noqa: F401
            except ModuleNotFoundError:
                return "sqlite:///./app.db"
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
