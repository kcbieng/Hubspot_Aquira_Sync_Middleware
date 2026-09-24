from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.settings import get_settings

Base = declarative_base()
settings = get_settings()
_url = settings.effective_database_url
_engine_kwargs: dict = {"future": True, "pool_pre_ping": True}
if _url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs["pool_recycle"] = 1800
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20
engine = create_engine(_url, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Import the model module so metadata is registered before creating tables.
from app.db import models  # noqa: F401

Base.metadata.create_all(bind=engine)


def _ensure_columns() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    statements: list[str] = []
    if "team_map" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("team_map")}
        if "hubspot_owner_id" not in existing:
            statements.append("ALTER TABLE team_map ADD COLUMN hubspot_owner_id VARCHAR(100)")
        if "hubspot_owner_name" not in existing:
            statements.append("ALTER TABLE team_map ADD COLUMN hubspot_owner_name VARCHAR(255)")
    if "owner_map" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("owner_map")}
        if "aquira_sales_rep_id" not in existing:
            statements.append("ALTER TABLE owner_map ADD COLUMN aquira_sales_rep_id VARCHAR(100)")
    if "app_user" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("app_user")}
        if "role_locked" not in existing:
            statements.append("ALTER TABLE app_user ADD COLUMN role_locked BOOLEAN DEFAULT 0")
        if "disabled" not in existing:
            statements.append("ALTER TABLE app_user ADD COLUMN disabled BOOLEAN DEFAULT 0")
        if "sso_subject" not in existing:
            statements.append("ALTER TABLE app_user ADD COLUMN sso_subject VARCHAR(255)")
    dead_letter_present = "dead_letter" in inspector.get_table_names()
    if dead_letter_present:
        existing = {col["name"] for col in inspector.get_columns("dead_letter")}
        for name, ddl in (
            ("hubspot_id", "VARCHAR(100)"),
            # NOT NULL DEFAULT keeps even an INSERT from not-yet-restarted old
            # code honest during a rolling deploy: a NULL status would make the
            # row invisible to every dead-letter query while the page's badge
            # still counted it as "retrying".
            ("status", "VARCHAR(20) NOT NULL DEFAULT 'open'"),
            ("last_attempt_at", "TIMESTAMP"),
            ("next_retry_at", "TIMESTAMP"),
            ("resolved_at", "TIMESTAMP"),
            ("resolution", "VARCHAR(255)"),
        ):
            if name not in existing:
                statements.append(f"ALTER TABLE dead_letter ADD COLUMN {name} {ddl}")
    # DDL races: web and worker containers both import app.db, so on the very
    # first boot after an upgrade the loser of each ALTER sees "duplicate
    # column". Savepoint + tolerate: the other process already did it.
    if statements:
        with engine.begin() as conn:
            for sql in statements:
                try:
                    with conn.begin_nested():
                        conn.execute(text(sql))
                except Exception:  # noqa: BLE001 - race loser; converged either way
                    pass
    if dead_letter_present:
        _backfill_dead_letter()


def _backfill_dead_letter() -> None:
    """Idempotent heals for rows that predate the status/hubspot_id columns —
    run on EVERY start, not only beside the ALTER, so a half-migrated database
    or an old worker's NULL inserts converge instead of vanishing from the UI."""
    import json

    from sqlalchemy import text

    hubspot_updates: list[dict] = []
    with engine.begin() as conn:
        conn.execute(text("UPDATE dead_letter SET status = 'open' WHERE status IS NULL"))
        for row_id, payload in conn.execute(
            text(
                "SELECT id, payload_json FROM dead_letter "
                "WHERE entity_type = 'client' AND aquira_id IS NULL AND hubspot_id IS NULL "
                "AND payload_json IS NOT NULL"
            )
        ).fetchall():
            try:
                hid = str((json.loads(payload) or {}).get("_hubspotId") or "").strip()[:100]
            except (TypeError, ValueError):
                continue
            if hid:
                hubspot_updates.append({"i": int(row_id), "h": hid})
        for bind in hubspot_updates:
            conn.execute(text("UPDATE dead_letter SET hubspot_id = :h WHERE id = :i"), bind)


_ensure_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


__all__ = ["Base", "SessionLocal", "get_db"]
