from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        protected_namespaces=("model_",),
    )

    app_name: str = "HubQuira"
    environment: str = "development"
    log_level: str = "INFO"
    timezone: str = "America/Chicago"
    public_base_url: str = ""

    aquira_base_url: str = "https://aquira2go.kcbieng.org/Aquira_WebAPI"
    aquira_username: str = ""
    aquira_password: str = ""
    hubspot_access_token: str = ""
    hubspot_client_secret: str = ""
    aquira_webhook_secret: str = ""
    database_url: str = "sqlite:///./app.db"
    sync_interval_minutes: int = 30
    whatif: bool = True
    sync_calls: bool = False
    sync_create_aquira_client: bool = True
    settings_fernet_key: str = ""
    ui_username: str = "admin"
    ui_password: str = "admin"
    bootstrap_hubspot: bool = True
    aquira_team_attribute: str = "HubSpot Team"
    hubquira_role: str = "all"

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
