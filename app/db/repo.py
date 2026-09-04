from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db.models import AppSetting, DeadLetter, JobEvent, SyncCursor, SyncRun, SyncRunItem
from app.settings import decrypt_secret, encrypt_secret, get_settings


def _as_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


class Repo:
    def __init__(self, session: Session | None = None):
        self.session = session or SessionLocal()

    def set_setting(self, key: str, value: str | None) -> None:
        row = self.session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
        if row is None:
            row = AppSetting(key=key)
        row.value_enc = encrypt_secret(value, get_settings().settings_fernet_key)
        self.session.add(row)
        self.session.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
        if row is None or row.value_enc in (None, ""):
            return None
        return decrypt_secret(row.value_enc, get_settings().settings_fernet_key)

    def set_cursor(self, job: str, last_started: str | datetime | None = None, last_finished: str | datetime | None = None, last_error: str | None = None, last_success_at: str | datetime | None = None) -> SyncCursor:
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

    def add_dead_letter(self, entity_type: str, aquira_id: str | int | None, error: str, payload: Any | None = None, attempts: int = 0) -> None:
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

    def add_run(self, trigger: str, whatif: bool, status: str = "pending") -> SyncRun:
        run = SyncRun(trigger=trigger, whatif=whatif, status=status)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def add_revenue_period(self, period: dict[str, Any]) -> None:
        from app.db.models import RevenuePeriod

        aquira_id = str(period.get("aquira_id"))
        row = self.session.execute(select(RevenuePeriod).where(RevenuePeriod.aquira_id == aquira_id)).scalar_one_or_none()
        if row is None:
            row = RevenuePeriod(aquira_id=aquira_id)
        row.contract_id = int(period.get("contract_id") or row.contract_id or 0)
        row.period = str(period.get("period") or row.period)
        row.amount = float(period.get("amount") or row.amount or 0)
        row.station = str(period.get("station") or row.station or "ALL")
        row.station_id = int(period.get("station_id") or row.station_id or 0)
        row.kind = str(period.get("kind") or row.kind or "booked")
        row.contract_cd = period.get("contract_cd") or row.contract_cd
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        self.session.commit()

    def get_revenue_periods_for_contract(self, contract_id: int | str) -> list[dict[str, Any]]:
        from app.db.models import RevenuePeriod

        rows = self.session.execute(
            select(RevenuePeriod).where(RevenuePeriod.contract_id == int(contract_id)).order_by(RevenuePeriod.period.asc())
        ).scalars().all()
        return [{
            "aquira_id": row.aquira_id,
            "period": row.period,
            "amount": row.amount,
            "station": row.station,
            "station_id": row.station_id,
            "kind": row.kind,
            "contract_id": row.contract_id,
            "contract_cd": row.contract_cd,
        } for row in rows]

    def delete_stale_revenue_periods(self, contract_id: int | str, keep_ids: set[str]) -> None:
        from app.db.models import RevenuePeriod

        rows = self.session.execute(select(RevenuePeriod).where(RevenuePeriod.contract_id == int(contract_id))).scalars().all()
        for row in rows:
            if row.aquira_id not in keep_ids:
                self.session.delete(row)
        self.session.commit()

    def add_run_item(self, run_id: int, entity_type: str, aquira_id: str | int | None, hubspot_id: str | None, action: str, diff_json: Any | None = None, error: str | None = None) -> SyncRunItem:
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
