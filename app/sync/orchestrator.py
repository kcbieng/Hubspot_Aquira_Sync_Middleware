from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.hashutil import content_hash
from app.settings import get_settings
from app.sync.planner import (
    plan_companies,
    plan_contact_writebacks,
    plan_contacts,
    plan_deals,
    plan_identity_writebacks,
    plan_new_aquira_clients,
    plan_revenue,
)
from app.sync.whatif import SyncInProgress

logger = logging.getLogger(__name__)


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
                self.summary_json = None
                self.finished_at = None

        return _Run(1)

    def add_event(self, job: str, level: str, message: str, payload: Any | None = None) -> None:
        return None

    def add_run_item(
        self,
        run_id: int,
        entity_type: str,
        aquira_id: str | int | None,
        hubspot_id: str | None,
        action: str,
        diff_json: Any | None = None,
        error: str | None = None,
    ):
        return None

    def add_dead_letter(self, *args, **kwargs) -> None:
        return None

    def upsert_id_map(self, *args, **kwargs) -> None:
        return None

    def list_owner_maps(self) -> list[Any]:
        return []

    def get_id_maps(self, entity_type: str | None = None) -> list[Any]:
        return []

    def set_cursor(self, *args, **kwargs) -> None:
        return None

    def close(self) -> None:
        return None


GROUP_ENTITY = {
    "companies": "company",
    "contacts": "contact",
    "deals": "deal",
    "revenue": "revenue_period",
}


def _lookup_from_existing(existing: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for group, entity_type in GROUP_ENTITY.items():
        for row in existing.get(group) or []:
            properties = row.get("properties") or {}
            aquira_id = str(properties.get("aquira_id") or row.get("aquira_id") or "")
            ident = str(row.get("id") or row.get("hubspotId") or "")
            if aquira_id and ident:
                lookup[(entity_type, aquira_id)] = ident
    return lookup


@dataclass
class SyncContext:
    trigger: str = "manual"
    whatif: bool = True
    entities: list[str] | None = None
    aquira_id: str | None = None


def _index_existing(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_aquira: dict[str, dict[str, Any]] = {}
    by_email: dict[str, dict[str, Any]] = {}
    for row in rows:
        properties = row.get("properties") or {}
        entry = {
            "hubspotId": str(row.get("id") or row.get("hubspotId") or ""),
            "properties": properties,
            "hash": row.get("hash") or content_hash(properties),
        }
        aquira_id = str(properties.get("aquira_id") or row.get("aquira_id") or "")
        if aquira_id:
            by_aquira[aquira_id] = entry
        email = str(properties.get("email") or "").lower()
        if email:
            by_email[email] = entry
    return by_aquira, by_email


def empty_catalog() -> dict[str, list[dict[str, Any]]]:
    return {"clients": [], "contacts": [], "contracts": [], "reps": []}


def empty_existing() -> dict[str, list[dict[str, Any]]]:
    return {"companies": [], "contacts": [], "deals": [], "revenue": [], "unsynced": []}


class SyncOrchestrator:
    DEFAULT_ENTITIES = ["companies", "contacts", "deals"]
    ALIASES = {"clients": "companies", "contracts": "deals"}
    _active = False

    def acquire_lock(self) -> None:
        if SyncOrchestrator._active:
            raise SyncInProgress("sync is already running")
        SyncOrchestrator._active = True

    def release_lock(self) -> None:
        SyncOrchestrator._active = False

    def _normalize_entities(self, context: SyncContext) -> list[str]:
        if context.entities:
            return list(context.entities)
        return list(self.DEFAULT_ENTITIES)

    def _wanted(self, entities: list[str]) -> set[str]:
        wanted = {self.ALIASES.get(name, name) for name in entities}
        if "deals" in wanted:
            wanted.add("revenue")
        if "companies" in wanted or "contacts" in wanted:
            wanted.add("writeback")
        return wanted

    def build_plan(
        self,
        catalog: dict[str, list[dict[str, Any]]],
        existing: dict[str, list[dict[str, Any]]],
        entities: list[str],
        owner_by_aquira: dict[str, str],
        create_missing_clients: bool = False,
    ) -> list[dict[str, Any]]:
        wanted = self._wanted(entities)
        clients = catalog.get("clients") or []
        contacts = catalog.get("contacts") or []
        contracts = catalog.get("contracts") or []
        companies_by_aquira, _ = _index_existing(existing.get("companies") or [])
        contacts_by_aquira, contacts_by_email = _index_existing(existing.get("contacts") or [])
        deals_by_aquira, _ = _index_existing(existing.get("deals") or [])
        revenue_by_aquira, _ = _index_existing(existing.get("revenue") or [])
        client_name_by_id = {str(client.get("ID")): str(client.get("Name") or "") for client in clients}

        items: list[dict[str, Any]] = []
        if "companies" in wanted:
            items.extend(plan_companies(clients, companies_by_aquira))
        if "contacts" in wanted:
            items.extend(plan_contacts(contacts, contacts_by_aquira, contacts_by_email))
        if "deals" in wanted:
            items.extend(plan_deals(contracts, deals_by_aquira, owner_by_aquira, client_name_by_id))
        if "revenue" in wanted:
            items.extend(plan_revenue(contracts, revenue_by_aquira))
        if "writeback" in wanted:
            hs_companies = []
            for row in existing.get("companies") or []:
                properties = row.get("properties") or {}
                hs_companies.append(
                    {
                        "aquira_id": str(properties.get("aquira_id") or row.get("aquira_id") or ""),
                        "properties": properties,
                        "hubspotId": str(row.get("id") or row.get("hubspotId") or ""),
                        "name": properties.get("name") or "",
                    }
                )
            aquira_by_id = {str(client.get("ID")): client for client in clients}
            items.extend(plan_identity_writebacks([row for row in hs_companies if row.get("aquira_id")], aquira_by_id))

            hs_contacts = []
            for row in existing.get("contacts") or []:
                properties = row.get("properties") or {}
                hs_contacts.append(
                    {
                        "aquira_id": str(properties.get("aquira_id") or row.get("aquira_id") or ""),
                        "properties": properties,
                        "hubspotId": str(row.get("id") or row.get("hubspotId") or ""),
                        "name": f"{properties.get('firstname') or ''} {properties.get('lastname') or ''}".strip(),
                    }
                )
            aquira_contacts = {str(contact.get("ID")): contact for contact in contacts}
            items.extend(plan_contact_writebacks([row for row in hs_contacts if row.get("aquira_id")], aquira_contacts))
            if create_missing_clients:
                items.extend(plan_new_aquira_clients(existing.get("unsynced") or []))
        return items

    def _owner_map(self, repo: Any) -> dict[str, str]:
        mapping: dict[str, str] = {}
        lister = getattr(repo, "list_owner_maps", None)
        if not callable(lister):
            return mapping
        try:
            rows = lister()
        except Exception:
            return mapping
        if not isinstance(rows, (list, tuple)):
            return mapping
        for row in rows:
            enabled = getattr(row, "enabled", None)
            if enabled is None and isinstance(row, dict):
                enabled = row.get("enabled")
            owner_id = getattr(row, "hubspot_owner_id", None)
            if owner_id is None and isinstance(row, dict):
                owner_id = row.get("hubspot_owner_id")
            aquira_id = getattr(row, "aquira_user_id", None)
            if aquira_id is None and isinstance(row, dict):
                aquira_id = row.get("aquira_user_id")
            if enabled and owner_id and aquira_id:
                mapping[str(aquira_id)] = str(owner_id)
        return mapping

    def _pull_live(
        self,
        repo: Any,
        aquira: Any | None,
        hubspot: Any | None,
        aquira_id: str | None,
        warnings: list[str],
    ) -> tuple[Any | None, Any | None, dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
        from app.aquira.client import AquiraSessionClient
        from app.hubspot.client import HubSpotClient

        settings = get_settings()
        catalog = empty_catalog()
        existing = empty_existing()

        if aquira is None and settings.aquira_username and settings.aquira_password:
            aquira = AquiraSessionClient()
        if hubspot is None and settings.hubspot_access_token:
            hubspot = HubSpotClient()

        if aquira is not None:
            try:
                if hasattr(aquira, "login"):
                    aquira.login()
                repo.add_event("sync", "INFO", "Aquira session opened", {"version": getattr(aquira, "version", None)})
                catalog = aquira.load_catalog(aquira_id) if hasattr(aquira, "load_catalog") else empty_catalog()
                repo.add_event(
                    "sync",
                    "INFO",
                    "Pulled Aquira catalog",
                    {
                        "clients": len(catalog.get("clients") or []),
                        "contacts": len(catalog.get("contacts") or []),
                        "contracts": len(catalog.get("contracts") or []),
                    },
                )
            except Exception as exc:
                logger.exception("Aquira catalog pull failed")
                warnings.append(f"Aquira pull failed: {exc}")
                repo.add_event("sync", "ERROR", f"Aquira pull failed: {exc}")

        if hubspot is not None:
            try:
                if settings.bootstrap_hubspot and hasattr(hubspot, "ensure_crm_schema"):
                    schema = hubspot.ensure_crm_schema()
                    warnings.extend(schema.get("warnings") or [])
                    if schema.get("created"):
                        repo.add_event("sync", "INFO", "Bootstrapped HubSpot properties", schema.get("created"))
                if hasattr(hubspot, "projection"):
                    projection = hubspot.projection()
                    existing["companies"] = projection.get("companies") or []
                    existing["contacts"] = projection.get("contacts") or []
                    existing["deals"] = projection.get("deals") or []
                    existing["revenue"] = projection.get("revenue") or []
                if settings.sync_create_aquira_client and hasattr(hubspot, "companies_without_aquira") and not aquira_id:
                    try:
                        existing["unsynced"] = [
                            {
                                "id": row.get("id"),
                                "hubspotId": row.get("id"),
                                "properties": row.get("properties") or {},
                                "aquira_id": None,
                            }
                            for row in hubspot.companies_without_aquira()
                        ]
                    except Exception as exc:
                        warnings.append(f"Could not list HubSpot companies without aquira_id: {exc}")
            except Exception as exc:
                warnings.append(f"HubSpot pull failed: {exc}")
                repo.add_event("sync", "ERROR", f"HubSpot pull failed: {exc}")

        return aquira, hubspot, catalog, existing

    def _hubspot_type(self, entity_type: str, hubspot: Any) -> str:
        if entity_type == "company":
            return "companies"
        if entity_type == "contact":
            return "contacts"
        if entity_type == "deal":
            return "deals"
        if entity_type == "revenue_period":
            return getattr(hubspot, "revenue_object_type", "revenue_period") if hubspot else "revenue_period"
        return entity_type

    def _resolve_hubspot_id(self, lookup: dict[str, str], aquira_id: str | None) -> str | None:
        if not aquira_id:
            return None
        return lookup.get(str(aquira_id))

    def apply_item(self, item: dict[str, Any], aquira: Any | None, hubspot: Any | None, lookup: dict[tuple[str, str], str]) -> dict[str, Any]:
        if item.get("action") == "skip":
            return item

        if item.get("writeback") and item.get("action") == "create" and item.get("entityType") == "client":
            if aquira is not None:
                created = aquira.create_client(item.get("properties") or {})
                item["aquiraId"] = str(created.get("ID"))
                if hubspot is not None and item.get("hubspotId"):
                    hubspot.upsert_crm("companies", {"aquira_id": str(created.get("ID"))}, item.get("hubspotId"))
            return item

        if item.get("writeback") and item.get("entityType") in {"client", "contact"}:
            if aquira is None or not item.get("aquiraId"):
                return item
            if item.get("entityType") == "client":
                aquira.update_client_sparse(item["aquiraId"], item.get("properties") or {})
            else:
                fields = item.get("properties") or {}
                client_id = (item.get("associations") or {}).get("clientId") or fields.get("ClientID")
                if client_id and hasattr(aquira, "update_contact_sparse"):
                    aquira.update_contact_sparse(client_id, item["aquiraId"], fields)
                else:
                    aquira.update_client_sparse(item["aquiraId"], {"Email": fields.get("Email"), "Phone": fields.get("Phone")})
            return item

        if item.get("action") == "delete-stale" and item.get("hubspotId"):
            if hubspot is not None:
                hubspot.archive(self._hubspot_type(item["entityType"], hubspot), item["hubspotId"])
            return item

        if hubspot is None:
            return item

        hs_type = self._hubspot_type(item["entityType"], hubspot)
        properties = dict(item.get("properties") or {})
        if item.get("entityType") == "deal" and properties.get("dealstage") == "proposal" and hasattr(hubspot, "ensure_proposal_stage"):
            properties["dealstage"] = hubspot.ensure_proposal_stage()
        record = hubspot.upsert_crm(hs_type, properties, item.get("hubspotId"))
        item["hubspotId"] = record.get("id")
        lookup[(item["entityType"], str(item.get("aquiraId") or ""))] = str(record.get("id"))

        associations = item.get("associations") or {}
        if item.get("entityType") == "company" and associations.get("parentCompanyId"):
            parent = lookup.get(("company", str(associations.get("parentCompanyId"))))
            if parent and parent != item["hubspotId"]:
                hubspot.associate("companies", item["hubspotId"], "companies", parent, 14)
        if item.get("entityType") == "contact":
            for company_id in associations.get("companyIds") or []:
                resolved = lookup.get(("company", str(company_id)))
                if resolved:
                    hubspot.associate("contacts", item["hubspotId"], "companies", resolved, 1)
        if item.get("entityType") == "deal":
            for company_id in associations.get("companyIds") or []:
                resolved = lookup.get(("company", str(company_id)))
                if resolved:
                    hubspot.associate("deals", item["hubspotId"], "companies", resolved, 5)
        if item.get("entityType") == "revenue_period":
            deal_id = lookup.get(("deal", str(associations.get("dealId") or "")))
            if deal_id:
                hubspot.associate(hs_type, item["hubspotId"], "deals", deal_id)
            for company_id in associations.get("companyIds") or []:
                resolved = lookup.get(("company", str(company_id)))
                if resolved:
                    hubspot.associate(hs_type, item["hubspotId"], "companies", resolved)
        return item

    def run(
        self,
        context: SyncContext,
        repo: Any | None = None,
        *,
        aquira: Any | None = None,
        hubspot: Any | None = None,
        catalog: dict[str, list[dict[str, Any]]] | None = None,
        existing: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        self.acquire_lock()
        owned_repo = repo is None
        if repo is None:
            from app.db.repo import Repo

            repo = Repo()
        started_at = datetime.utcnow()
        entities = self._normalize_entities(context)
        run = repo.add_run(context.trigger, context.whatif, status="running")
        repo.add_event(
            "sync",
            "INFO",
            "sync started",
            {"trigger": context.trigger, "whatif": context.whatif, "entities": entities, "aquira_id": context.aquira_id},
        )
        warnings: list[str] = []
        live_aquira = aquira
        live_hubspot = hubspot
        try:
            settings = get_settings()
            if catalog is None or existing is None:
                live_aquira, live_hubspot, pulled_catalog, pulled_existing = self._pull_live(
                    repo, live_aquira, live_hubspot, context.aquira_id, warnings
                )
                catalog = catalog if catalog is not None else pulled_catalog
                existing = existing if existing is not None else pulled_existing
            catalog = catalog or empty_catalog()
            existing = existing or empty_existing()

            items = self.build_plan(
                catalog,
                existing,
                entities,
                self._owner_map(repo),
                create_missing_clients=bool(settings.sync_create_aquira_client),
            )

            lookup = _lookup_from_existing(existing)

            applied: list[dict[str, Any]] = []
            if not items:
                if warnings:
                    message = "; ".join(warnings)
                    if hasattr(run, "status"):
                        run.status = "error"
                    if hasattr(run, "error"):
                        run.error = message
                    if hasattr(run, "finished_at"):
                        run.finished_at = datetime.utcnow()
                    if hasattr(repo, "session"):
                        repo.session.commit()
                    repo.add_event("sync", "ERROR", "sync failed before planning", {"warnings": warnings})
                    return {
                        "status": "error",
                        "trigger": context.trigger,
                        "whatif": context.whatif,
                        "entities": entities,
                        "started_at": started_at.isoformat(),
                        "run_id": getattr(run, "id", None),
                        "counts": {},
                        "warnings": warnings,
                        "item_count": 0,
                        "error": message,
                    }
                for entity in entities:
                    repo.add_run_item(
                        run.id,
                        entity,
                        aquira_id=context.aquira_id,
                        hubspot_id=None,
                        action="planned",
                        diff_json={"entity": entity, "whatif": context.whatif, "mode": "planned", "note": "no matching records"},
                    )
                    applied.append({"entityType": entity, "action": "planned", "name": entity, "diffs": [], "properties": {}})
            else:
                for item in items:
                    try:
                        next_item = item if context.whatif else self.apply_item(item, live_aquira, live_hubspot, lookup)
                        applied.append(next_item)
                        repo.add_run_item(
                            run.id,
                            next_item.get("entityType"),
                            aquira_id=next_item.get("aquiraId"),
                            hubspot_id=next_item.get("hubspotId"),
                            action=next_item.get("action") or "planned",
                            diff_json={
                                "name": next_item.get("name"),
                                "diffs": next_item.get("diffs") or [],
                                "properties": next_item.get("properties") or {},
                                "associations": next_item.get("associations"),
                                "writeback": next_item.get("writeback") or False,
                                "whatif": context.whatif,
                            },
                            error=next_item.get("error"),
                        )
                        if not context.whatif and next_item.get("aquiraId") and next_item.get("hubspotId") and not next_item.get("writeback"):
                            try:
                                repo.upsert_id_map(
                                    next_item.get("entityType"),
                                    str(next_item.get("aquiraId")),
                                    next_item.get("entityType"),
                                    str(next_item.get("hubspotId")),
                                    content_hash(next_item.get("properties") or {}),
                                )
                            except Exception:
                                pass
                    except Exception as exc:
                        message = str(exc)
                        failed = {**item, "action": "error", "error": message}
                        applied.append(failed)
                        repo.add_run_item(
                            run.id,
                            item.get("entityType"),
                            aquira_id=item.get("aquiraId"),
                            hubspot_id=item.get("hubspotId"),
                            action="error",
                            diff_json={"name": item.get("name"), "diffs": item.get("diffs") or [], "properties": item.get("properties") or {}},
                            error=message,
                        )
                        try:
                            repo.add_dead_letter(item.get("entityType"), item.get("aquiraId"), message, item.get("properties"), attempts=1)
                        except Exception:
                            pass

            counts: dict[str, int] = {}
            for item in applied:
                action = str(item.get("action") or "planned")
                counts[action] = counts.get(action, 0) + 1
            error_count = counts.get("error", 0)
            if error_count and error_count == len(applied):
                status = "error"
            elif error_count:
                status = "partial"
            else:
                status = "success"
            summary = {"counts": counts, "itemCount": len(applied), "warnings": warnings}
            if hasattr(run, "status"):
                run.status = status
            if hasattr(run, "summary_json"):
                import json

                run.summary_json = json.dumps(summary)
            if hasattr(run, "finished_at"):
                run.finished_at = datetime.utcnow()
            if hasattr(run, "error") and status != "success":
                run.error = f"{error_count} item error(s)" if error_count else None
            if hasattr(repo, "session"):
                repo.session.commit()
            try:
                repo.set_cursor(
                    "poll",
                    last_started=started_at,
                    last_finished=datetime.utcnow(),
                    last_success_at=datetime.utcnow() if status != "error" else None,
                    last_error=None if status == "success" else f"{error_count} item error(s)",
                )
            except Exception:
                pass
            repo.add_event("sync", "INFO", "sync completed", {"trigger": context.trigger, "whatif": context.whatif, "entities": entities, "counts": counts, "status": status})
            return {
                "status": status,
                "trigger": context.trigger,
                "whatif": context.whatif,
                "entities": entities,
                "started_at": started_at.isoformat(),
                "run_id": getattr(run, "id", None),
                "counts": counts,
                "warnings": warnings,
                "item_count": len(applied),
            }
        except Exception as exc:
            repo.add_event("sync", "ERROR", "sync failed", {"error": str(exc), "entities": entities})
            if hasattr(run, "status"):
                run.status = "error"
            if hasattr(run, "error"):
                run.error = str(exc)
            if hasattr(run, "finished_at"):
                run.finished_at = datetime.utcnow()
            if hasattr(repo, "session"):
                repo.session.commit()
            try:
                repo.set_cursor("poll", last_started=started_at, last_finished=datetime.utcnow(), last_error=str(exc))
            except Exception:
                pass
            raise
        finally:
            if live_aquira is not None and hasattr(live_aquira, "logout"):
                try:
                    live_aquira.logout()
                except Exception:
                    pass
            closer = getattr(repo, "close", None)
            if owned_repo and callable(closer):
                try:
                    closer()
                except Exception:
                    pass
            self.release_lock()
