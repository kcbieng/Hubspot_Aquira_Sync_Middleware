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
    sync_writeback: bool = False
    sync_create_aquira_client: bool = False
    settings_fernet_key: str = ""
    ui_username: str = "admin"
    ui_password: str = "admin"
    bootstrap_hubspot: bool = True
    aquira_team_attribute: str = "Hubspot_Team"
    hubquira_role: str = "web"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    match_digest_enabled: bool = True
    sso_enabled: bool = False
    oidc_issuer: str = ""  # e.g. https://login.microsoftonline.com/<tenant-id>/v2.0
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    sso_admin_group: str = ""  # Entra object id of HQ-Admins; membership => admin
    sso_sales_group: str = ""  # Entra object id of HQ-Sales; membership => sales (else denied)
    teams_webhook_url: str = ""  # M365 Workflows "post to channel when webhook request is received"

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
