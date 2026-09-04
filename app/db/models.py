from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SyncCursor(Base):
    __tablename__ = "sync_cursor"

    job: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_started: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_finished: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IdMap(Base):
    __tablename__ = "id_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    aquira_id: Mapped[str] = mapped_column(String(100))
    hubspot_object_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    hubspot_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    aquira_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobEvent(Base):
    __tablename__ = "job_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[str] = mapped_column(String(100))
    level: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeadLetter(Base):
    __tablename__ = "dead_letter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    entity_type: Mapped[str] = mapped_column(String(50))
    aquira_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OwnerMap(Base):
    __tablename__ = "owner_map"

    aquira_user_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    aquira_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    aquira_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hubspot_owner_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hubspot_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hubspot_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    suggested: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RevenuePeriod(Base):
    __tablename__ = "revenue_period"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(Integer)
    aquira_id: Mapped[str] = mapped_column(String(100), unique=True)
    period: Mapped[str] = mapped_column(String(20))
    amount: Mapped[float] = mapped_column(Integer)
    station: Mapped[str] = mapped_column(String(100), default="ALL")
    station_id: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(20), default="booked")
    contract_cd: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SyncRun(Base):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trigger: Mapped[str] = mapped_column(String(50), default="manual")
    whatif: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncRunItem(Base):
    __tablename__ = "sync_run_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer)
    entity_type: Mapped[str] = mapped_column(String(50))
    aquira_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hubspot_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action: Mapped[str] = mapped_column(String(20))
    diff_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
