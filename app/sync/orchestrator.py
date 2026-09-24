from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.hashutil import content_hash
from app.settings import get_settings
from app.mapping.matching import apply_match_rules, match_clients
from app.sync.planner import (
    plan_companies,
    plan_contact_writebacks,
    plan_contacts,
    plan_deals,
    plan_identity_writebacks,
    plan_inactive_deal_archives,
    plan_missing_deals,
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
            "archived": bool(row.get("archived")),
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
        if "writeback" in wanted and not get_settings().sync_writeback:
            wanted.discard("writeback")
        return wanted

    def build_plan(
        self,
        catalog: dict[str, list[dict[str, Any]]],
        existing: dict[str, list[dict[str, Any]]],
        entities: list[str],
        owner_by_aquira: dict[str, str],
        create_missing_clients: bool = False,
        aquira_id: str | None = None,
        allow_prune: bool = False,
        snapshots: dict[str, dict[str, Any]] | None = None,
        match_rules: dict[str, list[dict[str, Any]]] | None = None,
        match_exclusions: dict[str, set[tuple[str, str]]] | None = None,
        create_blocked_ids: set[str] | None = None,
        stage_map: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        wanted = self._wanted(entities)
        clients = catalog.get("clients") or []
        contacts = catalog.get("contacts") or []
        contracts = catalog.get("contracts") or []
        snap = snapshots or {}
        companies_by_aquira, _ = _index_existing(existing.get("companies") or [])
        contacts_by_aquira, contacts_by_email = _index_existing(existing.get("contacts") or [])
        deals_by_aquira, _ = _index_existing(existing.get("deals") or [])
        revenue_by_aquira, _ = _index_existing(existing.get("revenue") or [])
        client_name_by_id = {str(client.get("ID")): str(client.get("Name") or "") for client in clients}

        items: list[dict[str, Any]] = []
        linked_hubspot_ids: set[str] = set()
        if "companies" in wanted:
            # Lead-first business flow: sales creates the company in HubSpot
            # before Aquira knows it exists. Match instead of duplicating —
            # with the admin's ordered rules when present, the built-in
            # domain/name heuristic otherwise.
            unlinked_rows = [
                row
                for row in (existing.get("companies") or [])
                if not str((row.get("properties") or {}).get("aquira_id") or "").strip()
            ]
            rules = [r for r in ((match_rules or {}).get("company") or []) if isinstance(r, dict)]
            banned = set((match_exclusions or {}).get("company") or set())
            if rules:
                links, suggestions = apply_match_rules(clients, unlinked_rows, rules, banned)
            else:
                links, suggestions = match_clients(clients, unlinked_rows)
                if banned:
                    links = {k: v for k, v in links.items() if (k, v[0]) not in banned}
                    suggestions = [s for s in suggestions if (s["aquiraId"], s["hubspotId"]) not in banned]
            rows_by_id = {str(row.get("id") or row.get("hubspotId") or ""): row for row in unlinked_rows}
            for cid, (hid, _rule) in links.items():
                row = rows_by_id.get(hid) or {}
                props = row.get("properties") or {}
                companies_by_aquira[cid] = {"hubspotId": hid, "properties": props, "hash": content_hash(props)}
            linked_hubspot_ids = {hid for hid, _ in links.values()}
            company_items = plan_companies(clients, companies_by_aquira)
            for citem in company_items:
                match = links.get(str(citem.get("aquiraId")))
                if match:
                    citem["matchedBy"] = match[1]
            items.extend(company_items)
            items.extend(self._match_notices(suggestions, "company"))
        if "contacts" in wanted:
            contact_rules = [r for r in ((match_rules or {}).get("contact") or []) if isinstance(r, dict)]
            if contact_rules:
                unlinked_contacts = [
                    row
                    for row in (existing.get("contacts") or [])
                    if not str((row.get("properties") or {}).get("aquira_id") or "").strip()
                ]
                clinks, csuggestions = apply_match_rules(
                    contacts, unlinked_contacts, contact_rules, set((match_exclusions or {}).get("contact") or set())
                )
                crows = {str(row.get("id") or row.get("hubspotId") or ""): row for row in unlinked_contacts}
                for cid, (hid, _rule) in clinks.items():
                    props = (crows.get(hid) or {}).get("properties") or {}
                    contacts_by_aquira[cid] = {"hubspotId": hid, "properties": props, "hash": content_hash(props)}
                contact_items = plan_contacts(contacts, contacts_by_aquira, contacts_by_email)
                for citem in contact_items:
                    match = clinks.get(str(citem.get("aquiraId")))
                    if match:
                        citem["matchedBy"] = match[1]
                items.extend(contact_items)
                items.extend(self._match_notices(csuggestions, "contact"))
            else:
                items.extend(plan_contacts(contacts, contacts_by_aquira, contacts_by_email))
        if "deals" in wanted:
            items.extend(
                plan_deals(
                    contracts,
                    deals_by_aquira,
                    owner_by_aquira,
                    client_name_by_id,
                    snap.get("deal") or {},
                    stage_map or None,
                )
            )
            if aquira_id is None:
                catalog_deal_ids = {str(row.get("ID")) for row in contracts if row.get("ID") is not None}
                items.extend(
                    plan_missing_deals(deals_by_aquira, catalog_deal_ids, allow_archive=allow_prune)
                )
                # Deactivated-in-Aquira is the tenant's delete gesture.
                items.extend(
                    plan_inactive_deal_archives(contracts, deals_by_aquira, allow_archive=allow_prune)
                )
        if "revenue" in wanted:
            # A contract whose line detail failed to load is excluded from the prune
            # scope: it is in this run but produced zero periods, so leaving it in
            # would archive real revenue months and re-create them next run.
            # Booked-but-inactive contracts are also excluded: their periods are
            # held intact for historical modelling. Inactive PROPOSALS stay in
            # scope with no desired periods, which purges them.
            in_scope = {
                str(row.get("ID"))
                for row in contracts
                if row.get("ID") is not None
                and not row.get("_detail_failed")
                and not (row.get("IsActive") is False and row.get("IsContract"))
            }
            items.extend(
                plan_revenue(
                    contracts,
                    revenue_by_aquira,
                    prune_stale=allow_prune and bool(in_scope),
                    only_contract_ids=in_scope or None,
                )
            )
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
            items.extend(
                plan_identity_writebacks(
                    [row for row in hs_companies if row.get("aquira_id")],
                    aquira_by_id,
                    snap.get("company") or {},
                )
            )

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
            items.extend(
                plan_contact_writebacks(
                    [row for row in hs_contacts if row.get("aquira_id")],
                    aquira_contacts,
                    snap.get("contact") or {},
                )
            )
            if create_missing_clients:
                # Companies already auto-linked this run already got their
                # aquira_id from the match — do not also create an Aquira client
                # for the same lead.
                unmatched_unsynced = [
                    row
                    for row in (existing.get("unsynced") or [])
                    if str(row.get("id") or row.get("hubspotId") or "") not in linked_hubspot_ids
                ]
                items.extend(
                    plan_new_aquira_clients(unmatched_unsynced, blocked_hubspot_ids=create_blocked_ids)
                )
        return items

    @staticmethod
    def _match_notices(suggestions: list[dict[str, Any]], kind: str = "company") -> list[dict[str, Any]]:
        """Rule/heuristic suggestions become notice items: visible in the run
        and in warnings, applied as nothing. The same notices are recorded as
        MatchSuggestion rows so the people who own the record can fix them on
        /ui/matches — confirm by setting the record's Aquira ID property on
        HubSpot, or click Link there and the next sync inherits it."""
        notices: list[dict[str, Any]] = []
        for s in suggestions:
            score_part = f" (score {s['score']})" if s.get("score") else ""
            notices.append(
                {
                    "entityType": "match-suggestion",
                    "suggestionEntity": kind,
                    "aquiraId": s["aquiraId"],
                    "hubspotId": s["hubspotId"],
                    "action": "notice",
                    "name": f'{s["clientName"]} ≈ {s["companyName"]}',
                    "diffs": [],
                    "properties": {},
                    "match": s,
                    "warning": (
                        f"Match needs a human: Aquira '{s['clientName']}' ({s['aquiraId']}) "
                        f"≈ HubSpot '{s['companyName']}' ({s['hubspotId']}) via {s['method']}"
                        f"{score_part} — {s['reason']}. To link: set that HubSpot record's Aquira ID "
                        f"property to {s['aquiraId']}; the next sync inherits the link."
                    ),
                }
            )
        return notices

    @staticmethod
    def _record_suggestions(
        repo: Any,
        items: list[dict[str, Any]],
        catalog: dict[str, list[dict[str, Any]]],
        existing: dict[str, list[dict[str, Any]]],
        run_id: int | None,
    ) -> None:
        """Persist notices as MatchSuggestion rows (in whatif too — they are
        information, not writes) and auto-resolve stale ones. The assignee is
        the Aquira sales rep's mapped HubSpot-user email when we know it, so
        the digest reaches the person who can answer, not a general inbox."""
        if not hasattr(repo, "record_match_suggestion"):
            return
        rep_emails: dict[str, str] = {}
        try:
            for row in repo.list_owner_maps() or []:
                email = str(getattr(row, "hubspot_email", None) or getattr(row, "aquira_email", None) or "").strip()
                if not email:
                    continue
                for key in (getattr(row, "aquira_user_id", None), getattr(row, "aquira_sales_rep_id", None)):
                    if key:
                        rep_emails[str(key).strip().lower()] = email
        except Exception:
            rep_emails = {}
        clients = {str(row.get("ID")): row for row in catalog.get("clients") or []}
        contacts = {str(row.get("ID")): row for row in catalog.get("contacts") or []}
        for item in items:
            if item.get("entityType") != "match-suggestion":
                continue
            s = item.get("match") or {}
            kind = str(item.get("suggestionEntity") or "company")
            aid = str(item.get("aquiraId") or "")
            hid = str(item.get("hubspotId") or "")
            if not aid or not hid:
                continue
            row = clients.get(aid) if kind == "company" else contacts.get(aid)
            owner_client = row if kind == "company" else clients.get(str((row or {}).get("ClientID") or ""))
            rep = str((owner_client or {}).get("SalesRepID") or "").strip().lower()
            try:
                repo.record_match_suggestion(
                    kind, aid, hid,
                    aquira_name=s.get("clientName"), hubspot_name=s.get("companyName"),
                    method=s.get("method"), reason=s.get("reason"),
                    score=int(s.get("score") or 0),
                    assignee_email=rep_emails.get(rep) or None,
                    run_id=run_id,
                )
            except Exception:
                pass
        try:
            for kind, rows_key in (("company", "companies"), ("contact", "contacts")):
                linked = {
                    str((row.get("properties") or {}).get("aquira_id") or "").strip()
                    for row in (existing.get(rows_key) or [])
                }
                linked.discard("")
                if linked and hasattr(repo, "resolve_suggestions_for_links"):
                    repo.resolve_suggestions_for_links(kind, linked)
        except Exception:
            pass

    def _owner_map(self, repo: Any, reps: list[dict[str, Any]] | None = None) -> dict[str, str]:
        from app.mapping.owners import expand_owner_lookup

        lister = getattr(repo, "list_owner_maps", None)
        if not callable(lister):
            return expand_owner_lookup([], reps)
        try:
            rows = lister()
        except Exception:
            return expand_owner_lookup([], reps)
        if not isinstance(rows, (list, tuple)):
            return expand_owner_lookup([], reps)
        return expand_owner_lookup(list(rows), reps)

    def _owner_by_name(self, repo: Any) -> dict[str, str]:
        from app.mapping.owners import _normalize_name

        mapping: dict[str, str] = {}
        lister = getattr(repo, "list_owner_maps", None)
        if not callable(lister):
            return mapping
        try:
            rows = lister()
        except Exception:
            return mapping
        for row in rows or []:
            enabled = getattr(row, "enabled", None)
            if enabled is None and isinstance(row, dict):
                enabled = row.get("enabled")
            owner_id = getattr(row, "hubspot_owner_id", None)
            if owner_id is None and isinstance(row, dict):
                owner_id = row.get("hubspot_owner_id")
            name = getattr(row, "aquira_name", None)
            if name is None and isinstance(row, dict):
                name = row.get("aquira_name")
            key = _normalize_name(name)
            if enabled and owner_id and key:
                mapping[key] = str(owner_id)
        return mapping

    def _team_map(self, repo: Any) -> dict[str, str]:
        from app.mapping.teams import normalize_team_key

        mapping: dict[str, str] = {}
        lister = getattr(repo, "list_team_maps", None)
        if not callable(lister):
            return mapping
        try:
            rows = lister()
        except Exception:
            return mapping
        for row in rows or []:
            enabled = getattr(row, "enabled", None)
            if enabled is None and isinstance(row, dict):
                enabled = row.get("enabled")
            team_id = getattr(row, "hubspot_team_id", None)
            if team_id is None and isinstance(row, dict):
                team_id = row.get("hubspot_team_id")
            key = getattr(row, "aquira_key", None)
            if key is None and isinstance(row, dict):
                key = row.get("aquira_key") or row.get("aquira_label")
            if enabled and team_id and key:
                mapping[normalize_team_key(key)] = str(team_id)
        return mapping

    def _team_owner_map(self, repo: Any) -> dict[str, str]:
        mapping: dict[str, str] = {}
        lister = getattr(repo, "list_team_maps", None)
        if not callable(lister):
            return mapping
        try:
            rows = lister()
        except Exception:
            return mapping
        for row in rows or []:
            enabled = getattr(row, "enabled", None)
            if enabled is None and isinstance(row, dict):
                enabled = row.get("enabled")
            team_id = getattr(row, "hubspot_team_id", None)
            if team_id is None and isinstance(row, dict):
                team_id = row.get("hubspot_team_id")
            owner_id = getattr(row, "hubspot_owner_id", None)
            if owner_id is None and isinstance(row, dict):
                owner_id = row.get("hubspot_owner_id")
            if enabled and team_id and owner_id:
                mapping[str(team_id)] = str(owner_id)
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
                    if hasattr(hubspot, "archived_deals"):
                        try:
                            live_ids = {
                                str((row.get("properties") or {}).get("aquira_id") or "")
                                for row in existing["deals"]
                            }
                            for row in hubspot.archived_deals():
                                if str((row.get("properties") or {}).get("aquira_id") or "") not in live_ids:
                                    existing["deals"].append(row)
                        except Exception as exc:
                            # Without the archived view a reactivated deal would
                            # look absent and be re-created as a duplicate.
                            warnings.append(f"Could not list archived HubSpot deals: {exc}")
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
                    if existing["unsynced"] and hasattr(hubspot, "known_property_names"):
                        try:
                            if "aquira_create_as" not in hubspot.known_property_names("companies"):
                                warnings.append(
                                    "HubSpot is missing the 'aquira_create_as' property — no clients will be "
                                    "created in Aquira until the schema is bootstrapped (the dropdown IS the "
                                    "approval gate now)"
                                )
                        except Exception:
                            pass
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

    @staticmethod
    def _persist_snapshot(
        repo: Any,
        item: dict[str, Any],
        clients_by_aid: dict[str, dict[str, Any]],
        contacts_by_aid: dict[str, dict[str, Any]],
    ) -> None:
        """Record the last-sync state (both sides) for one mapped record, so the
        next run can distinguish its own source-transitions from human edits."""
        if item.get("writeback") or str(item.get("action") or "") not in {"create", "update", "skip"}:
            return
        etype = str(item.get("entityType") or "")
        aid = str(item.get("aquiraId") or "")
        if etype not in {"company", "contact", "deal"} or not aid:
            return
        hubspot_side = item.get("properties") or {}
        aquira_side: dict[str, Any] = {}
        if etype == "deal":
            aquira_side = item.get("aquiraDerived") or {}
        elif etype == "company":
            row = clients_by_aid.get(aid)
            if row:
                aquira_side = {k: str(row.get(k) or "") for k in ("Name", "Phone", "Website", "PhysicalAddress")}
        else:
            row = contacts_by_aid.get(aid)
            if row:
                aquira_side = {k: str(row.get(k) or "") for k in ("FirstName", "LastName", "Email", "Phone")}
        try:
            repo.save_snapshot(etype, aid, hubspot_side, aquira_side)
        except Exception:
            pass

    def apply_item(self, item: dict[str, Any], aquira: Any | None, hubspot: Any | None, lookup: dict[tuple[str, str], str]) -> dict[str, Any]:
        if item.get("action") == "notice":
            return item  # operator information only; nothing to apply on either side
        if item.get("action") == "skip":
            ident = str(item.get("hubspotId") or lookup.get((item.get("entityType"), str(item.get("aquiraId") or ""))) or "").strip()
            if not ident:
                item["action"] = "create"
                item["hubspotId"] = None
            else:
                item["hubspotId"] = ident
                lookup[(item["entityType"], str(item.get("aquiraId") or ""))] = ident
                if hubspot is not None:
                    self._apply_associations(item, hubspot, lookup)
                return item

        if item.get("writeback") and item.get("action") == "create" and item.get("entityType") == "client":
            if aquira is not None:
                created = aquira.create_client(item.get("properties") or {}, party_type=item.get("createAs") or "account")
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

        if item.get("action") == "unarchive" and item.get("hubspotId"):
            # Reactivated contract: restore the archived record we made for it,
            # then update it in this same pass rather than creating a twin.
            if hubspot is not None:
                hubspot.restore(self._hubspot_type(item["entityType"], hubspot), item["hubspotId"])
            item["action"] = "update"
            item["unarchived"] = True

        if item.get("action") in {"delete-stale", "archive"} and item.get("hubspotId"):
            if hubspot is not None:
                hubspot.archive(self._hubspot_type(item["entityType"], hubspot), item["hubspotId"])
            return item

        if hubspot is None:
            return item

        hs_type = self._hubspot_type(item["entityType"], hubspot)
        properties = dict(item.get("properties") or {})
        stage_settings = get_settings()
        # The legacy rescue belongs to the DEFAULT-pipeline world where the
        # literal "proposal" token survived planning: an explicit proposal
        # mapping has already replaced the token, and a custom pipeline's ids
        # cannot be found by ensure_proposal_stage (it picks from the account's
        # FIRST pipeline). But it must still fire when the operator mapped
        # only won/lost and proposals deliberately stay semantic.
        if (
            item.get("entityType") == "deal"
            and properties.get("dealstage") == "proposal"
            and not (stage_settings.hubspot_stage_proposal or "").strip()
            and not (stage_settings.hubspot_deal_pipeline or "").strip()
            and hasattr(hubspot, "ensure_proposal_stage")
        ):
            properties["dealstage"] = hubspot.ensure_proposal_stage()
        record = hubspot.upsert_crm(hs_type, properties, item.get("hubspotId"))
        item["hubspotId"] = record.get("id")
        lookup[(item["entityType"], str(item.get("aquiraId") or ""))] = str(record.get("id"))
        self._apply_associations(item, hubspot, lookup)
        return item

    def _apply_associations(self, item: dict[str, Any], hubspot: Any, lookup: dict[tuple[str, str], str]) -> None:
        associations = item.get("associations") or {}
        hubspot_id = str(item.get("hubspotId") or "")
        if not hubspot_id:
            return
        if item.get("entityType") == "company" and associations.get("parentCompanyId"):
            parent = lookup.get(("company", str(associations.get("parentCompanyId"))))
            if parent and parent != hubspot_id:
                hubspot.associate("companies", hubspot_id, "companies", parent, 14)
                hubspot.associate("companies", parent, "companies", hubspot_id, 13)
        if item.get("entityType") == "contact":
            for company_id in associations.get("companyIds") or []:
                resolved = lookup.get(("company", str(company_id)))
                if resolved:
                    hubspot.associate("contacts", hubspot_id, "companies", resolved, 1)
        if item.get("entityType") == "deal":
            for company_id in associations.get("companyIds") or []:
                resolved = lookup.get(("company", str(company_id)))
                if resolved:
                    hubspot.associate("deals", hubspot_id, "companies", resolved, 5)
        if item.get("entityType") == "revenue_period":
            hs_type = self._hubspot_type("revenue_period", hubspot)
            deal_id = lookup.get(("deal", str(associations.get("dealId") or "")))
            if deal_id:
                hubspot.associate(hs_type, hubspot_id, "deals", deal_id)
            for company_id in dict.fromkeys(str(value) for value in (associations.get("companyIds") or []) if str(value or "").strip()):
                resolved = lookup.get(("company", str(company_id)))
                if resolved:
                    hubspot.associate(hs_type, hubspot_id, "companies", resolved)

    def run(
        self,
        context: SyncContext,
        repo: Any | None = None,
        *,
        aquira: Any | None = None,
        hubspot: Any | None = None,
        catalog: dict[str, list[dict[str, Any]]] | None = None,
        existing: dict[str, list[dict[str, Any]]] | None = None,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        self.acquire_lock()
        owned_repo = repo is None
        if repo is None:
            from app.db.repo import Repo

            repo = Repo()
        started_at = datetime.utcnow()
        entities = self._normalize_entities(context)
        run = None
        if run_id:
            run = repo.get_run(run_id)
        if run is None:
            run = repo.add_run(context.trigger, context.whatif, status="running")
        else:
            run.status = "running"
            run.started_at = started_at
            if hasattr(repo, "session"):
                repo.session.commit()
        repo.add_event(
            "sync",
            "INFO",
            "sync started",
            {"trigger": context.trigger, "whatif": context.whatif, "entities": entities, "aquira_id": context.aquira_id},
        )
        warnings: list[str] = []
        notices: list[str] = []
        live_aquira = aquira
        live_hubspot = hubspot
        try:
            try:
                from app.runtime import apply_db_overlay

                # Live settings for a long-running worker: the sync must see
                # the stage mapping and toggles the operator saved since this
                # process booted (the UI is a different container).
                apply_db_overlay()
            except Exception:
                logger.debug("settings overlay refresh failed; using boot-time values", exc_info=True)
            settings = get_settings()
            if catalog is None or existing is None:
                live_aquira, live_hubspot, pulled_catalog, pulled_existing = self._pull_live(
                    repo, live_aquira, live_hubspot, context.aquira_id, warnings
                )
                catalog = catalog if catalog is not None else pulled_catalog
                existing = existing if existing is not None else pulled_existing
            catalog = catalog or empty_catalog()
            existing = existing or empty_existing()
            from app.mapping.teams import apply_team_ids, team_attribute_names

            teams_by_name: dict[str, str] = {}
            teams_by_id: dict[str, str] = {}
            owner_team_by_owner_id: dict[str, str] = {}
            if live_hubspot is not None and hasattr(live_hubspot, "list_teams"):
                try:
                    from app.mapping.teams import normalize_team_key

                    for team in live_hubspot.list_teams() or []:
                        name = str(team.get("name") or "").strip()
                        ident = str(team.get("id") or "")
                        if name and ident:
                            teams_by_name[normalize_team_key(name)] = ident
                            teams_by_id[ident] = name
                except Exception:
                    teams_by_name = {}
                    teams_by_id = {}
            if live_hubspot is not None and hasattr(live_hubspot, "owner_primary_team_map"):
                try:
                    owner_team_by_owner_id = live_hubspot.owner_primary_team_map() or {}
                except Exception:
                    owner_team_by_owner_id = {}
            owner_lookup = self._owner_map(repo, catalog.get("reps") or [])
            apply_team_ids(
                catalog,
                self._team_map(repo),
                teams_by_name=teams_by_name,
                attribute_names=team_attribute_names(settings.aquira_team_attribute),
                owner_by_aquira=owner_lookup,
                owner_team_by_owner_id=owner_team_by_owner_id,
                team_owner_by_team_id=self._team_owner_map(repo),
                owner_by_name=self._owner_by_name(repo),
                teams_by_id=teams_by_id,
            )

            integrity = catalog.get("_integrity") or {}
            allow_prune = bool(integrity.get("certified"))
            if "revenue" in self._wanted(entities) and not allow_prune:
                detail = {
                    "failed_reads": integrity.get("failed_reads", 0),
                    "failed_calls": (integrity.get("failed_calls") or [])[:8],
                    "truncated_sources": integrity.get("truncated_sources") or [],
                    "detail_failures": integrity.get("detail_failures", 0),
                }
                causes: list[str] = []
                if detail["truncated_sources"]:
                    causes.append(
                        f"{len(detail['truncated_sources'])} source(s) hit the Aquira row cap "
                        f"({', '.join(detail['truncated_sources'][:4])}) — records beyond it are invisible"
                    )
                if detail["failed_reads"]:
                    causes.append(f"{detail['failed_reads']} failed read(s)")
                if detail["detail_failures"]:
                    causes.append(f"{detail['detail_failures']} contract detail load(s) failed")
                message = (
                    "Revenue pruning suppressed: the Aquira pull is not certified complete "
                    f"({'; '.join(causes) or 'reason unknown'}); "
                    f"{integrity.get('contract_rows', 0)} contract(s) were visible to this run. "
                    "Existing revenue_period records were left untouched."
                )
                # A notice, not a warning: a suppressed prune is a safe outcome and
                # must not flip an otherwise clean run to status="error".
                notices.append(message)
                repo.add_event("sync", "WARN", message, detail)

            try:
                raw_snapshots = repo.get_snapshots() if hasattr(repo, "get_snapshots") else {}
                snapshots = raw_snapshots if isinstance(raw_snapshots, dict) else {}
            except Exception:
                snapshots = {}
            try:
                match_rules: dict[str, list[dict[str, Any]]] = {}
                for entity_type in ("company", "contact"):
                    raw_rules = repo.active_match_rules(entity_type) if hasattr(repo, "active_match_rules") else []
                    match_rules[entity_type] = [r for r in (raw_rules or []) if isinstance(r, dict)]
            except Exception:
                match_rules = {}
            try:
                match_exclusions = {
                    entity_type: set(repo.exclusions_for(entity_type)) if hasattr(repo, "exclusions_for") else set()
                    for entity_type in ("company", "contact")
                }
            except Exception:
                match_exclusions = {}
            create_blocked: set[str] = set()
            if settings.sync_create_aquira_client and not context.aquira_id:
                try:
                    create_blocked = (
                        set(repo.open_client_create_failures()) if hasattr(repo, "open_client_create_failures") else set()
                    )
                except Exception:
                    create_blocked = set()
            stage_map = {
                key: str(getattr(settings, f"hubspot_stage_{key}") or "").strip()
                for key in ("proposal", "won", "lost")
            }
            stage_map = {
                ("closedwon" if k == "won" else "closedlost" if k == "lost" else k): v
                for k, v in stage_map.items()
                if v
            }
            if (settings.hubspot_deal_pipeline or "").strip():
                stage_map["pipeline"] = settings.hubspot_deal_pipeline.strip()
            items = self.build_plan(
                catalog,
                existing,
                entities,
                owner_lookup,
                create_missing_clients=bool(settings.sync_create_aquira_client),
                aquira_id=context.aquira_id,
                allow_prune=allow_prune,
                snapshots=snapshots,
                match_rules=match_rules,
                match_exclusions=match_exclusions,
                create_blocked_ids=create_blocked,
                stage_map=stage_map or None,
            )
            clients_by_aid = {str(row.get("ID")): row for row in catalog.get("clients") or []}
            contacts_by_aid = {str(row.get("ID")): row for row in catalog.get("contacts") or []}
            for item in items:
                warning = item.get("warning")
                if warning:
                    warnings.append(str(warning))
                    repo.add_event("sync", "WARN", str(warning), {"aquira_id": item.get("aquiraId"), "entity": item.get("entityType")})


            lookup = _lookup_from_existing(existing)

            applied: list[dict[str, Any]] = []
            reconciled_pairs: list[dict[str, Any]] = []
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
                        "notices": notices,
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
                                "hubspotId": next_item.get("hubspotId"),
                                "diffs": next_item.get("diffs") or [],
                                "properties": next_item.get("properties") or {},
                                "associations": next_item.get("associations"),
                                "writeback": next_item.get("writeback") or False,
                                "whatif": context.whatif,
                                "warning": next_item.get("warning"),
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
                        if not context.whatif:
                            self._persist_snapshot(repo, next_item, clients_by_aid, contacts_by_aid)
                            # A live write success closes any pending failed-write
                            # row for this record — reconciliation is then automatic.
                            # ONLY a real write: skip/notice/archive performed no
                            # update, and resolving a row on those would tell the
                            # operator a broken record was fixed.
                            if str(next_item.get("action") or "") in {"create", "update"}:
                                reconciled_pairs.append(
                                    {
                                        "entity_type": str(next_item.get("entityType") or ""),
                                        "aquira_id": str(next_item.get("aquiraId") or ""),
                                        "hubspot_id": str(next_item.get("hubspotId") or ""),
                                    }
                                )
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
                            repo.add_dead_letter(
                                item.get("entityType"),
                                item.get("aquiraId"),
                                message,
                                item.get("properties") or {},
                                attempts=1,
                                hubspot_id=item.get("hubspotId"),
                            )
                        except Exception:
                            pass

            try:
                if reconciled_pairs and hasattr(repo, "resolve_dead_letters"):
                    closed = repo.resolve_dead_letters(reconciled_pairs, f"written by sync #{getattr(run, 'id', None)}")
                    if closed:
                        repo.add_event("sync", "INFO", f"{closed} pending failed-write row(s) auto-resolved", {})
            except Exception:
                logger.debug("dead-letter auto-resolve failed", exc_info=True)

            try:
                self._record_suggestions(repo, items, catalog, existing, getattr(run, "id", None))
            except Exception:
                logger.exception("Could not record match suggestions")

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
            summary = {"counts": counts, "itemCount": len(applied), "warnings": warnings, "notices": notices}
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
            try:
                from app import alerts

                alerts.report_run(
                    status=status,
                    error_count=error_count,
                    notices=notices,
                    warnings=warnings,
                    run_id=getattr(run, "id", None),
                    whatif=bool(context.whatif),
                )
            except Exception:
                logger.debug("alert dispatch failed", exc_info=True)
            return {
                "status": status,
                "trigger": context.trigger,
                "whatif": context.whatif,
                "entities": entities,
                "started_at": started_at.isoformat(),
                "run_id": getattr(run, "id", None),
                "counts": counts,
                "warnings": warnings,
                "notices": notices,
                "item_count": len(applied),
            }
        except Exception as exc:
            repo.add_event("sync", "ERROR", "sync failed", {"error": str(exc), "entities": entities})
            try:
                from app import alerts

                alerts.report_run(
                    status="error",
                    run_id=getattr(run, "id", None) if "run" in dir() else None,
                    whatif=bool(context.whatif),
                    exception=f"sync crashed: {exc}",
                )
            except Exception:
                logger.debug("alert dispatch failed", exc_info=True)
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
