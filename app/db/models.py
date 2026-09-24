from __future__ import annotations

from datetime import datetime

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


class EntitySnapshot(Base):
    """Last-sync state of one mapped record: what HubSpot held after our write
    (``hubspot_json``) and what Aquira said at that moment (``aquira_json``).
    These are the baselines that make human edits and source changes tellable
    apart — the content hash alone cannot tell who moved a value."""

    __tablename__ = "entity_snapshot"

    entity_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    aquira_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    hubspot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    aquira_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    aquira_sales_rep_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hubspot_owner_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hubspot_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hubspot_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    suggested: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TeamMap(Base):
    __tablename__ = "team_map"

    aquira_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    aquira_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    hubspot_team_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hubspot_team_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hubspot_owner_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hubspot_owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    suggested: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SyncRun(Base):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trigger: Mapped[str] = mapped_column(String(50), default="manual")
    whatif: Mapped[bool] = mapped_column(Boolean, default=True)
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


class AppUser(Base):
    """Named users so match reviews can be assigned, emailed, and permissioned.
    The env-configured admin (ui_username/ui_password) always works — this table
    can never lock everybody out, it can only add people."""

    __tablename__ = "app_user"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(20), default="sales")
    role_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sso_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MatchSuggestion(Base):
    """A possible company/contact duplicate that needs a human decision,
    persisted across runs so it can be emailed and reviewed on /ui/matches."""

    __tablename__ = "match_suggestion"

    entity_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    aquira_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    hubspot_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    aquira_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hubspot_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    method: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    assignee_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MatchExclusion(Base):
    """'Not a duplicate' — a pair a human said no to. The matcher skips these
    forever (until an admin removes the exclusion), so dismissals stick."""

    __tablename__ = "match_exclusion"

    entity_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    aquira_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    hubspot_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MatchRule(Base):
    """Ordered, rule-based company/contact matching (admin-managed).

    One rule = a NAME plus a list of field-pair conditions that must ALL match
    (AND). Rules are evaluated per entity_type in priority order: the first
    enabled rule that matches decides the outcome — ``link`` binds the records
    (writes aquira_id) or ``suggest`` surfaces them for a human. Exclusivity
    (one HubSpot record per Aquira entity and vice versa) is enforced by the
    engine, not the rule author."""

    __tablename__ = "match_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    conditions_json: Mapped[str] = mapped_column(Text, default="[]")
    on_match: Mapped[str] = mapped_column(String(20), default="link")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipt"

    message_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkQueue(Base):
    __tablename__ = "work_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(50), default="sync")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
