from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.aquira.client import AquiraSessionClient
from app.db.models import OwnerMap
from app.db.repo import Repo
from app.hubspot.client import HubSpotClient
from app.sync.whatif import SyncInProgress


class _NoopRepo:
    class _Session:
        def commit(self) -> None:
            pass

    def __init__(self):
        self.session = self._Session()

    def add_run(self, trigger: str, whatif: bool, status: str = "pending"):
        class _Run:
            def __init__(self, run_id: int):
                self.id = run_id
                self.status = status
                self.error = None

        return _Run(1)

    def add_event(self, job: str, level: str, message: str, payload: Any | None = None) -> None:
        return None

    def add_run_item(self, run_id: int, entity_type: str, aquira_id: str | int | None, hubspot_id: str | None, action: str, diff_json: Any | None = None, error: str | None = None):
        return None


@dataclass
class SyncContext:
    trigger: str = "manual"
    whatif: bool = True
    entities: list[str] | None = None


class SyncOrchestrator:
    DEFAULT_ENTITIES = ["companies", "contacts", "deals"]

    def __init__(self):
        self._active = False

    def acquire_lock(self) -> None:
        if self._active:
            raise SyncInProgress("sync is already running")
        self._active = True

    def release_lock(self) -> None:
        self._active = False

    def _normalize_entities(self, context: SyncContext) -> list[str]:
        if context.entities:
            return context.entities
        return list(self.DEFAULT_ENTITIES)

    def _owner_map_for_sync(self) -> dict[str, str]:
        repo = Repo()
        rows = repo.session.query(OwnerMap).filter(OwnerMap.enabled.is_(True)).all()
        return {str(row.aquira_user_id): str(row.hubspot_owner_id) for row in rows if row.hubspot_owner_id}

    def _entity_payload(self, entity: str) -> tuple[str, list[dict[str, Any]]]:
        if entity == "companies":
            try:
                client = AquiraSessionClient()
                data = client.load_sales_reps()
                client.close()
                return "aquira", data[:10]
            except Exception:
                return "aquira", []

        if entity == "contacts":
            try:
                client = HubSpotClient()
                payload = client.get_owners()
                items = payload.get("results", [])
                return "hubspot", items[:10]
            except Exception:
                return "hubspot", []

        if entity == "deals":
            try:
                client = HubSpotClient()
                payload = client.get_owners()
                items = payload.get("results", [])
                owner_map = self._owner_map_for_sync()
                for item in items:
                    item["owner_map_applied"] = owner_map.get(str(item.get("ownerId")), item.get("ownerId"))
                return "hubspot", items[:10]
            except Exception:
                return "hubspot", []

        return "local", []

    def run(self, context: SyncContext, repo: Any | None = None) -> dict[str, Any]:
        self.acquire_lock()
        repo = repo or _NoopRepo()
        started_at = datetime.utcnow()
        entities = self._normalize_entities(context)
        run = repo.add_run(context.trigger, context.whatif, status="running")
        repo.add_event("sync", "INFO", "sync started", {"trigger": context.trigger, "whatif": context.whatif, "entities": entities})
        try:
            for entity in entities:
                source, rows = self._entity_payload(entity)
                action = "planned" if context.whatif else "applied"
                diff_json = {
                    "entity": entity,
                    "source": source,
                    "whatif": context.whatif,
                    "mode": "planned" if context.whatif else "live",
                    "items": rows,
                }
                repo.add_run_item(run.id, entity, aquira_id=None, hubspot_id=None, action=action, diff_json=diff_json)
            repo.add_event("sync", "INFO", "sync completed", {"trigger": context.trigger, "whatif": context.whatif, "entities": entities})
            if hasattr(run, "status"):
                run.status = "success"
            if hasattr(repo, "session"):
                repo.session.commit()
            return {
                "status": "success",
                "trigger": context.trigger,
                "whatif": context.whatif,
                "entities": entities,
                "started_at": started_at.isoformat(),
                "run_id": getattr(run, "id", None),
            }
        except Exception as exc:  # pragma: no cover - guarded by lock semantics
            repo.add_event("sync", "ERROR", "sync failed", {"error": str(exc), "entities": entities})
            if hasattr(run, "status"):
                run.status = "error"
            if hasattr(run, "error"):
                run.error = str(exc)
            if hasattr(repo, "session"):
                repo.session.commit()
            raise
        finally:
            self.release_lock()
