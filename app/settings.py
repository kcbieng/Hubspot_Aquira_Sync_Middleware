from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
