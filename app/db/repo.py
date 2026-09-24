from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db.models import (
    AppSetting,
    AppUser,
    DeadLetter,
    EntitySnapshot,
    IdMap,
    JobEvent,
    MatchExclusion,
    MatchRule,
    MatchSuggestion,
    OwnerMap,
    SyncCursor,
    SyncRun,
    SyncRunItem,
    TeamMap,
    WebhookReceipt,
    WorkQueue,
)


def _as_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


class Repo:
    def __init__(self, session: Session | None = None):
        self._owns_session = session is None
        self.session = session or SessionLocal()

    def set_setting(self, key: str, value: str | None) -> None:
        row = self.session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
        if row is None:
            row = AppSetting(key=key)
        row.value_enc = value
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        self.session.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
        return row.value_enc if row else None

    def all_settings(self) -> dict[str, str | None]:
        rows = self.session.execute(select(AppSetting)).scalars().all()
        return {row.key: row.value_enc for row in rows}

    def set_cursor(
        self,
        job: str,
        last_started: str | datetime | None = None,
        last_finished: str | datetime | None = None,
        last_error: str | None = None,
        last_success_at: str | datetime | None = None,
    ) -> SyncCursor:
        row = self.session.execute(select(SyncCursor).where(SyncCursor.job == job)).scalar_one_or_none()
        if row is None:
            row = SyncCursor(job=job)
        row.last_started = _as_datetime(last_started)
        row.last_finished = _as_datetime(last_finished)
        row.last_error = last_error
        row.last_success_at = _as_datetime(last_success_at)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get_cursor(self, job: str) -> SyncCursor | None:
        return self.session.execute(select(SyncCursor).where(SyncCursor.job == job)).scalar_one_or_none()

    def add_event(self, job: str, level: str, message: str, payload: Any | None = None) -> None:
        self.session.add(
            JobEvent(
                job=job,
                level=level,
                message=message,
                payload_json=json.dumps(payload) if payload is not None else None,
            )
        )
        self.session.commit()

    def list_events(self, limit: int = 200) -> list[JobEvent]:
        return (
            self.session.execute(select(JobEvent).order_by(JobEvent.ts.desc()).limit(limit))
            .scalars()
            .all()
        )

    def add_dead_letter(
        self,
        entity_type: str,
        aquira_id: str | int | None,
        error: str,
        payload: Any | None = None,
        attempts: int = 0,
    ) -> None:
        self.session.add(
            DeadLetter(
                entity_type=entity_type,
                aquira_id=str(aquira_id) if aquira_id is not None else None,
                error=error,
                payload_json=json.dumps(payload) if payload is not None else None,
                attempts=attempts,
            )
        )
        self.session.commit()

    def list_dead_letters(self, limit: int = 200) -> list[DeadLetter]:
        return list(
            self.session.execute(select(DeadLetter).order_by(DeadLetter.id.desc()).limit(limit)).scalars().all()
        )

    def delete_dead_letter(self, dead_letter_id: int) -> None:
        row = self.session.get(DeadLetter, int(dead_letter_id))
        if row is not None:
            self.session.delete(row)
            self.session.commit()

    def bump_dead_letter(self, dead_letter_id: int) -> None:
        row = self.session.get(DeadLetter, int(dead_letter_id))
        if row is not None:
            row.attempts = (row.attempts or 0) + 1
            self.session.add(row)
            self.session.commit()

    def open_client_create_failures(self) -> set[str]:
        """HubSpot company ids whose client-creation attempt is still sitting in
        the dead-letter table — the create gate honors these and waits."""
        out: set[str] = set()
        rows = self.session.execute(
            select(DeadLetter).where(DeadLetter.entity_type == "client", DeadLetter.aquira_id.is_(None))
        ).scalars().all()
        for row in rows:
            try:
                payload = json.loads(row.payload_json) if row.payload_json else {}
            except (TypeError, ValueError):
                payload = {}
            hid = str((payload or {}).get("_hubspotId") or "").strip()
            if hid:
                out.add(hid)
        return out

    def add_run(self, trigger: str, whatif: bool, status: str = "pending") -> SyncRun:
        run = SyncRun(trigger=trigger, whatif=whatif, status=status)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def add_run_item(
        self,
        run_id: int,
        entity_type: str,
        aquira_id: str | int | None,
        hubspot_id: str | None,
        action: str,
        diff_json: Any | None = None,
        error: str | None = None,
    ) -> SyncRunItem:
        item = SyncRunItem(
            run_id=run_id,
            entity_type=entity_type,
            aquira_id=str(aquira_id) if aquira_id is not None else None,
            hubspot_id=hubspot_id,
            action=action,
            diff_json=json.dumps(diff_json) if diff_json is not None else None,
            error=error,
        )
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    def list_runs(self, limit: int = 50) -> list[SyncRun]:
        return (
            self.session.execute(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit))
            .scalars()
            .all()
        )

    def get_run(self, run_id: int) -> SyncRun | None:
        return self.session.get(SyncRun, run_id)

    def list_run_items(self, run_id: int) -> list[SyncRunItem]:
        return (
            self.session.execute(select(SyncRunItem).where(SyncRunItem.run_id == run_id).order_by(SyncRunItem.id.asc()))
            .scalars()
            .all()
        )

    def latest_run(self) -> SyncRun | None:
        return self.session.execute(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)).scalar_one_or_none()

    def upsert_id_map(
        self,
        entity_type: str,
        aquira_id: str,
        hubspot_object_type: str,
        hubspot_id: str,
        content_hash: str | None = None,
        aquira_version: int | None = None,
    ) -> IdMap:
        row = self.session.execute(
            select(IdMap).where(IdMap.entity_type == entity_type, IdMap.aquira_id == aquira_id)
        ).scalar_one_or_none()
        if row is None:
            row = IdMap(entity_type=entity_type, aquira_id=aquira_id)
        row.hubspot_object_type = hubspot_object_type
        row.hubspot_id = hubspot_id
        row.content_hash = content_hash
        row.aquira_version = aquira_version
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get_id_maps(self, entity_type: str | None = None) -> list[IdMap]:
        stmt = select(IdMap)
        if entity_type:
            stmt = stmt.where(IdMap.entity_type == entity_type)
        return self.session.execute(stmt).scalars().all()

    def get_snapshots(self) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
        """{entity_type: {aquira_id: {"hubspot": {...}, "aquira": {...}}}}"""
        out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        for row in self.session.execute(select(EntitySnapshot)).scalars().all():
            try:
                hubspot = json.loads(row.hubspot_json) if row.hubspot_json else {}
                aquira = json.loads(row.aquira_json) if row.aquira_json else {}
            except (TypeError, ValueError):
                continue
            out.setdefault(row.entity_type, {})[str(row.aquira_id)] = {"hubspot": hubspot, "aquira": aquira}
        return out

    def save_snapshot(self, entity_type: str, aquira_id: str, hubspot_side: dict[str, Any], aquira_side: dict[str, Any]) -> None:
        if not entity_type or not str(aquira_id or "").strip():
            return
        row = self.session.get(EntitySnapshot, (entity_type, str(aquira_id)))
        if row is None:
            row = EntitySnapshot(entity_type=entity_type, aquira_id=str(aquira_id))
        row.hubspot_json = json.dumps(hubspot_side or {}, default=str)
        row.aquira_json = json.dumps(aquira_side or {}, default=str)
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        self.session.commit()

    def list_owner_maps(self) -> list[OwnerMap]:
        return self.session.execute(select(OwnerMap)).scalars().all()

    # ---- match rules (ordered, admin-managed) ----
    def list_match_rules(self, entity_type: str | None = None) -> list[MatchRule]:
        stmt = select(MatchRule)
        if entity_type:
            stmt = stmt.where(MatchRule.entity_type == entity_type)
        return list(
            self.session.execute(stmt.order_by(MatchRule.entity_type, MatchRule.priority, MatchRule.id)).scalars().all()
        )

    def active_match_rules(self, entity_type: str) -> list[dict[str, Any]]:
        """Engine-ready: enabled rules in priority order, conditions parsed."""
        out: list[dict[str, Any]] = []
        for row in self.list_match_rules(entity_type):
            if not row.enabled:
                continue
            try:
                conditions = json.loads(row.conditions_json or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(conditions, list) or not conditions:
                continue
            out.append({"id": row.id, "name": row.name, "on_match": row.on_match, "conditions": conditions})
        return out

    def create_match_rule(self, entity_type: str, name: str, conditions: list[dict[str, Any]], on_match: str = "link") -> MatchRule:
        top = self.session.execute(
            select(func.max(MatchRule.priority)).where(MatchRule.entity_type == entity_type)
        ).scalar()
        row = MatchRule(
            entity_type=entity_type,
            name=name or "New rule",
            conditions_json=json.dumps(conditions),
            on_match=on_match if on_match in {"link", "suggest"} else "link",
            priority=(top or 0) + 1,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def update_match_rule(
        self,
        rule_id: int,
        *,
        name: str | None = None,
        conditions: list[dict[str, Any]] | None = None,
        on_match: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        row = self.session.get(MatchRule, rule_id)
        if row is None:
            return
        if name is not None:
            row.name = name
        if conditions:  # a rule with zero conditions is meaningless; never allow a wipe to empty
            row.conditions_json = json.dumps(conditions)
        if on_match is not None and on_match in {"link", "suggest"}:
            row.on_match = on_match
        if enabled is not None:
            row.enabled = enabled
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        self.session.commit()

    def delete_match_rule(self, rule_id: int) -> None:
        row = self.session.get(MatchRule, rule_id)
        if row is not None:
            self.session.delete(row)
            self.session.commit()

    def move_match_rule(self, rule_id: int, direction: str) -> None:
        row = self.session.get(MatchRule, rule_id)
        if row is None:
            return
        rows = self.list_match_rules(row.entity_type)
        idx = next((i for i, r in enumerate(rows) if r.id == rule_id), None)
        if idx is None:
            return
        swap_with = idx - 1 if direction == "up" else idx + 1
        if swap_with < 0 or swap_with >= len(rows):
            return
        other = rows[swap_with]
        row.priority, other.priority = other.priority, row.priority
        if row.priority == other.priority:  # seeded ties: force a strict difference
            if direction == "up":
                row.priority = other.priority - 1
            else:
                row.priority = other.priority + 1
        self.session.add(row)
        self.session.add(other)
        self.session.commit()

    def ensure_default_match_rules(self) -> int:
        """Seed the current-field reality once per entity type so the page is
        never empty; edits after that are the operator's own."""
        created = 0
        existing_types = {row.entity_type for row in self.list_match_rules()}
        defaults: dict[str, list[tuple[str, str, list[dict[str, str]]]]] = {
            "company": [
                ("Website domain", "link", [{"aquira_field": "Website", "hubspot_field": "domain", "mode": "domain"}]),
                ("Business name + phone", "link", [
                    {"aquira_field": "Name", "hubspot_field": "name", "mode": "normalized"},
                    {"aquira_field": "Phone", "hubspot_field": "phone", "mode": "phone"},
                ]),
                ("Business name", "link", [{"aquira_field": "Name", "hubspot_field": "name", "mode": "normalized"}]),
            ],
            "contact": [
                ("Email", "link", [{"aquira_field": "Email", "hubspot_field": "email", "mode": "exact"}]),
                ("Full name + phone", "suggest", [
                    {"aquira_field": "FirstName", "hubspot_field": "firstname", "mode": "normalized"},
                    {"aquira_field": "LastName", "hubspot_field": "lastname", "mode": "normalized"},
                    {"aquira_field": "Phone", "hubspot_field": "phone", "mode": "phone"},
                ]),
                ("Full name", "suggest", [
                    {"aquira_field": "FirstName", "hubspot_field": "firstname", "mode": "normalized"},
                    {"aquira_field": "LastName", "hubspot_field": "lastname", "mode": "normalized"},
                ]),
            ],
        }
        for entity_type, rules in defaults.items():
            if entity_type in existing_types:
                continue
            for priority, (name, on_match, conditions) in enumerate(rules):
                self.session.add(
                    MatchRule(
                        entity_type=entity_type,
                        name=name,
                        conditions_json=json.dumps(conditions),
                        on_match=on_match,
                        priority=priority,
                    )
                )
                created += 1
        if created:
            self.session.commit()
        return created

    def search_history(
        self,
        term: str = "",
        entity_type: str | None = None,
        limit: int = 200,
    ) -> list[tuple[SyncRunItem, SyncRun | None]]:
        """Record history lookup: every change the sync wrote for one Aquira id,
        HubSpot id, contract/client CD, or name fragment — joined to its run."""
        term = str(term or "").strip()
        stmt = (
            select(SyncRunItem, SyncRun)
            .outerjoin(SyncRun, SyncRunItem.run_id == SyncRun.id)
            .order_by(SyncRunItem.id.desc())
            .limit(limit)
        )
        if term:
            like = f"%{term}%"
            stmt = stmt.where(
                (SyncRunItem.aquira_id == term)
                | (SyncRunItem.hubspot_id == term)
                | (SyncRunItem.diff_json.like(like))
            )
        if entity_type:
            stmt = stmt.where(SyncRunItem.entity_type == entity_type)
        return [(row[0], row[1]) for row in self.session.execute(stmt).all()]

    def list_team_maps(self) -> list[TeamMap]:
        return self.session.execute(select(TeamMap)).scalars().all()

    # ---- users ----
    def list_users(self) -> list[AppUser]:
        return list(self.session.execute(select(AppUser).order_by(AppUser.email)).scalars().all())

    def get_user(self, email: str) -> AppUser | None:
        return self.session.get(AppUser, str(email or "").strip().lower())

    def upsert_user(self, email: str, name: str, role: str, password_hash: str) -> AppUser:
        email = str(email or "").strip().lower()
        row = self.session.get(AppUser, email)
        if row is None:
            row = AppUser(email=email)
        row.name = name or email.split("@", 1)[0]
        row.role = role if role in {"admin", "sales"} else "sales"
        if password_hash:
            row.password_hash = password_hash
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        self.session.commit()
        return row

    def delete_user(self, email: str) -> None:
        row = self.session.get(AppUser, str(email or "").strip().lower())
        if row is not None:
            self.session.delete(row)
            self.session.commit()

    def provision_sso_user(self, email: str, name: str, role: str, subject: str) -> tuple[AppUser | None, bool]:
        """Just-in-time provisioning at SSO login. Returns (user, allowed).
        disabled users are refused; role_locked users keep their local role —
        Entra membership still gates access, an admin just opted out of
        automatic role sync."""
        email = str(email or "").strip().lower()
        row = self.session.get(AppUser, email)
        if row is None:
            row = AppUser(email=email)
        if row.disabled:
            return row, False
        row.name = name or row.name or email.split("@", 1)[0]
        if not row.role_locked:
            row.role = role if role in {"admin", "sales"} else "sales"
        row.sso_subject = subject or row.sso_subject
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        self.session.commit()
        return row, True

    # ---- match suggestions & exclusions ----
    def record_match_suggestion(
        self,
        entity_type: str,
        aquira_id: str,
        hubspot_id: str,
        *,
        aquira_name: str | None,
        hubspot_name: str | None,
        method: str | None,
        reason: str | None,
        score: int,
        assignee_email: str | None,
        run_id: int | None,
    ) -> None:
        key = (entity_type, str(aquira_id), str(hubspot_id))
        row = self.session.get(MatchSuggestion, key)
        if row is None:
            row = MatchSuggestion(entity_type=entity_type, aquira_id=key[1], hubspot_id=key[2])
        elif row.status in {"dismissed", "linked"}:
            return  # a human already decided this pair; silence is the answer
        row.aquira_name = aquira_name
        row.hubspot_name = hubspot_name
        row.method = method
        row.reason = reason
        row.score = score
        row.assignee_email = assignee_email or row.assignee_email
        row.run_id = run_id
        row.status = "pending"
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        self.session.commit()

    def resolve_suggestions_for_links(self, entity_type: str, linked_aquira_ids: set[str]) -> None:
        """Once an Aquira id points at a HubSpot record (by any route), every
        pending suggestion for that id is resolved — no ghost rows."""
        if not linked_aquira_ids:
            return
        rows = self.session.execute(
            select(MatchSuggestion).where(
                MatchSuggestion.entity_type == entity_type,
                MatchSuggestion.status == "pending",
                MatchSuggestion.aquira_id.in_(sorted(linked_aquira_ids)),
            )
        ).scalars().all()
        for row in rows:
            row.status = "linked"
            row.updated_at = datetime.utcnow()
            self.session.add(row)
        if rows:
            self.session.commit()

    def list_suggestions(self, statuses: tuple[str, ...] = ("pending",)) -> list[MatchSuggestion]:
        return list(
            self.session.execute(
                select(MatchSuggestion)
                .where(MatchSuggestion.status.in_(statuses))
                .order_by(MatchSuggestion.updated_at.desc())
            ).scalars().all()
        )

    def set_suggestion_status(self, entity_type: str, aquira_id: str, hubspot_id: str, status: str) -> None:
        row = self.session.get(MatchSuggestion, (entity_type, str(aquira_id), str(hubspot_id)))
        if row is not None:
            row.status = status
            row.updated_at = datetime.utcnow()
            self.session.add(row)
            self.session.commit()

    def pending_suggestions_due(self, older_than: datetime) -> list[MatchSuggestion]:
        return list(
            self.session.execute(
                select(MatchSuggestion).where(
                    MatchSuggestion.status == "pending",
                    (MatchSuggestion.last_notified_at.is_(None)) | (MatchSuggestion.last_notified_at < older_than),
                )
            ).scalars().all()
        )

    def mark_suggestions_notified(self, rows: list[MatchSuggestion]) -> None:
        now = datetime.utcnow()
        for row in rows:
            row.last_notified_at = now
            self.session.add(row)
        self.session.commit()

    def add_match_exclusion(self, entity_type: str, aquira_id: str, hubspot_id: str, created_by: str | None) -> None:
        key = (entity_type, str(aquira_id), str(hubspot_id))
        if self.session.get(MatchExclusion, key) is None:
            self.session.add(MatchExclusion(entity_type=key[0], aquira_id=key[1], hubspot_id=key[2], created_by=created_by))
            self.session.commit()

    def list_match_exclusions(self) -> list[MatchExclusion]:
        return list(self.session.execute(select(MatchExclusion).order_by(MatchExclusion.created_at)).scalars().all())

    def removal_match_exclusion(self, entity_type: str, aquira_id: str, hubspot_id: str) -> None:
        row = self.session.get(MatchExclusion, (entity_type, str(aquira_id), str(hubspot_id)))
        if row is not None:
            self.session.delete(row)
            self.session.commit()

    def exclusions_for(self, entity_type: str) -> set[tuple[str, str]]:
        return {
            (row.aquira_id, row.hubspot_id)
            for row in self.list_match_exclusions()
            if row.entity_type == entity_type
        }

    def seen_webhook(self, message_id: str) -> bool:
        if not message_id:
            return False
        row = self.session.get(WebhookReceipt, message_id)
        return row is not None

    def mark_webhook(self, message_id: str) -> None:
        if not message_id:
            return
        if self.session.get(WebhookReceipt, message_id) is not None:
            return
        self.session.add(WebhookReceipt(message_id=message_id))
        self.session.commit()

    def add_job(self, kind: str, payload: Any, run_id: int | None = None) -> WorkQueue:
        row = WorkQueue(
            kind=kind,
            status="queued",
            payload_json=json.dumps(payload) if payload is not None else None,
            run_id=run_id,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def claim_job(self) -> WorkQueue | None:
        stmt = select(WorkQueue).where(WorkQueue.status == "queued").order_by(WorkQueue.id.asc()).limit(1)
        bind = self.session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        row.status = "running"
        row.started_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(row)
        return row

    def finish_job(self, job_id: int, status: str = "done", error: str | None = None) -> None:
        row = self.session.get(WorkQueue, job_id)
        if row is None:
            return
        row.status = status
        row.error = error
        row.finished_at = datetime.utcnow()
        self.session.commit()

    def active_job_count(self) -> int:
        return int(
            self.session.execute(
                select(func.count()).select_from(WorkQueue).where(WorkQueue.status.in_(("queued", "running")))
            ).scalar()
            or 0
        )

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

