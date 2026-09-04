from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.settings import get_settings

Base = declarative_base()
settings = get_settings()
engine = create_engine(settings.effective_database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Import the model module so metadata is registered before creating tables.
from app.db import models  # noqa: F401

Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


__all__ = ["Base", "SessionLocal", "get_db"]
