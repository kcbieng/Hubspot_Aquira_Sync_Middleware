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
    if "team_map" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("team_map")}
    statements = []
    if "hubspot_owner_id" not in existing:
        statements.append("ALTER TABLE team_map ADD COLUMN hubspot_owner_id VARCHAR(100)")
    if "hubspot_owner_name" not in existing:
        statements.append("ALTER TABLE team_map ADD COLUMN hubspot_owner_name VARCHAR(255)")
    if not statements:
        return
    with engine.begin() as conn:
        for sql in statements:
            conn.execute(text(sql))


_ensure_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


__all__ = ["Base", "SessionLocal", "get_db"]
