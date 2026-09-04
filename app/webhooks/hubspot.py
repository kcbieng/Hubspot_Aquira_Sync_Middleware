from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any


IDENTITY_COMPANY_FIELDS = {"name", "phone", "domain", "website", "address", "city", "state"}
IDENTITY_CONTACT_FIELDS = {"firstname", "lastname", "email", "phone"}


def verify_hubspot_signature(method: bytes, uri: bytes, payload: bytes, timestamp: str, signature: str, client_secret: str) -> bool:
    if not timestamp or not signature or not client_secret:
        return False
    try:
        ts_value = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts_value) > 300:
        return False

    spec_payload = b"".join([method, uri, payload, timestamp.encode("utf-8")])
    legacy_payload = payload + timestamp.encode("utf-8")
    expected_spec = base64.b64encode(hmac.new(client_secret.encode("utf-8"), spec_payload, hashlib.sha256).digest()).decode("utf-8")
    expected_legacy = base64.b64encode(hmac.new(client_secret.encode("utf-8"), legacy_payload, hashlib.sha256).digest()).decode("utf-8")
    return hmac.compare_digest(expected_spec, signature) or hmac.compare_digest(expected_legacy, signature)


def webhook_event_is_duplicate(message_id: str, seen_ids: set[str]) -> bool:
    return message_id in seen_ids


def signature_candidates(request) -> list[bytes]:
    path = request.url.path.encode("utf-8")
    full = str(request.url).encode("utf-8")
    candidates = [path, full]
    from app.settings import get_settings

    public = (get_settings().public_base_url or "").rstrip("/")
    if public:
        candidates.append(f"{public}{request.url.path}".encode("utf-8"))
    return candidates


def verify_request_signature(request, payload: bytes, timestamp: str, signature: str, client_secret: str) -> bool:
    method = request.method.encode("utf-8")
    return any(verify_hubspot_signature(method, uri, payload, timestamp, signature, client_secret) for uri in signature_candidates(request))


def normalize_events(body_json: Any) -> tuple[str | None, list[dict[str, Any]]]:
    if isinstance(body_json, list):
        events = [row for row in body_json if isinstance(row, dict)]
        message_id = str(events[0].get("eventId") or events[0].get("messageId") or "") if events else None
        return message_id or None, events
    if isinstance(body_json, dict):
        message_id = str(body_json.get("messageId") or body_json.get("eventId") or "") or None
        nested = body_json.get("events")
        if isinstance(nested, list):
            return message_id, [row for row in nested if isinstance(row, dict)]
        return message_id, [body_json]
    return None, []


def identity_targets(events: list[dict[str, Any]]) -> tuple[set[str], set[str], bool]:
    companies: set[str] = set()
    contacts: set[str] = set()
    create_missing = False
    for event in events:
        sub = str(event.get("subscriptionType") or "")
        oid = str(event.get("objectId") or "")
        prop = str(event.get("propertyName") or "")
        if not oid:
            continue
        if sub.startswith("company."):
            if sub.endswith("creation"):
                create_missing = True
                companies.add(oid)
            elif not prop or prop in IDENTITY_COMPANY_FIELDS:
                companies.add(oid)
        elif sub.startswith("contact.") and (not prop or prop in IDENTITY_CONTACT_FIELDS):
            contacts.add(oid)
    return companies, contacts, create_missing


def process_hubspot_identity_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    companies, contacts, create_missing = identity_targets(events)
    if not companies and not contacts:
        return {"processed": 0, "runs": []}
    from app.db.repo import Repo
    from app.settings import get_settings
    from app.sync.orchestrator import SyncContext, SyncOrchestrator

    settings = get_settings()
    if not settings.hubspot_access_token:
        return {"processed": 0, "runs": [], "ignored": "hubspot not configured"}
    repo = Repo()
    orchestrator = SyncOrchestrator()
    runs: list[Any] = []
    aquira_ids: list[str] = []
    try:
        from app.hubspot.client import HubSpotClient

        hubspot = HubSpotClient()
        for ident in companies:
            try:
                record = hubspot.get_record("companies", ident, ["aquira_id", "name", "phone", "domain", "address"])
            except Exception:
                continue
            aquira_id = str((record.get("properties") or {}).get("aquira_id") or "")
            if aquira_id:
                aquira_ids.append(aquira_id)
        for ident in contacts:
            try:
                record = hubspot.get_record(
                    "contacts", ident, ["aquira_id", "aquira_client_id", "firstname", "lastname", "email", "phone"]
                )
            except Exception:
                continue
            aquira_id = str((record.get("properties") or {}).get("aquira_id") or "")
            if aquira_id:
                aquira_ids.append(aquira_id)
    except Exception as exc:
        repo.add_event("webhook", "ERROR", f"HubSpot identity lookup failed: {exc}")
        return {"processed": 0, "runs": [], "error": str(exc)}

    seen: set[str] = set()
    for aquira_id in aquira_ids:
        if aquira_id in seen:
            continue
        seen.add(aquira_id)
        try:
            result = orchestrator.run(
                SyncContext(trigger="webhook", whatif=bool(settings.whatif), entities=["writeback"], aquira_id=aquira_id),
                repo=repo,
            )
            runs.append(result)
        except Exception as exc:
            repo.add_event("webhook", "ERROR", f"identity writeback failed for {aquira_id}: {exc}")
    if create_missing and settings.sync_create_aquira_client and not aquira_ids:
        try:
            result = orchestrator.run(
                SyncContext(trigger="webhook", whatif=bool(settings.whatif), entities=["writeback"]),
                repo=repo,
            )
            runs.append(result)
        except Exception as exc:
            repo.add_event("webhook", "ERROR", f"create-missing client sync failed: {exc}")
    return {"processed": len(runs), "runs": runs, "aquira_ids": sorted(seen)}
