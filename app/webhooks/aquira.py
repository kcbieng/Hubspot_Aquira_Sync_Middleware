from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

ID_KEYS = {
    "id",
    "contractid",
    "contract_id",
    "proposalid",
    "proposal_id",
    "entityid",
    "entity_id",
    "objectid",
    "object_id",
    "aquiraid",
    "aquira_id",
    "recordid",
    "record_id",
}
CD_KEYS = {"contractcd", "contract_cd", "proposalcd", "proposal_cd", "cd", "code"}
EVENT_KEYS = {"event", "eventtype", "event_type", "notification", "name", "type", "subject", "title", "action"}
ID_RE = re.compile(
    r"(?:contract|proposal|entity|record|aquira)(?:\s*(?:id|cd|#|number|no\.?))?\s*[:=#]?\s*([A-Za-z0-9_-]{1,32})",
    re.I,
)


def parse_body(raw: bytes, content_type: str | None, query: dict[str, list[str]] | None = None) -> Any:
    text = raw.decode("utf-8", "replace").strip() if raw else ""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if text:
        if "json" in ctype or text[:1] in "{[":
            try:
                return json.loads(text)
            except ValueError:
                pass
        if "xml" in ctype or text.startswith("<"):
            try:
                return _xml_to_dict(ET.fromstring(text))
            except ET.ParseError:
                pass
        if "x-www-form-urlencoded" in ctype or "=" in text and "&" in text and text[:1] not in "{[<":
            parsed = parse_qs(text, keep_blank_values=True)
            return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}
    if query:
        return {key: values[0] if len(values) == 1 else values for key, values in query.items() if key != "token"}
    return text or None


def _xml_to_dict(node: ET.Element) -> dict[str, Any]:
    children = list(node)
    payload: dict[str, Any] = {key: value for key, value in node.attrib.items()}
    if children:
        grouped: dict[str, list[Any]] = {}
        for child in children:
            grouped.setdefault(child.tag, []).append(_xml_to_dict(child))
        for key, values in grouped.items():
            payload[key] = values[0] if len(values) == 1 else values
    text = (node.text or "").strip()
    if text:
        payload["#text"] = text
        if len(payload) == 1:
            return {"tag": node.tag, "value": text, node.tag: text}
        payload[node.tag] = text
    return payload or {"tag": node.tag}


def extract_notification(payload: Any, raw_text: str = "") -> dict[str, Any]:
    ids: list[str] = []
    cds: list[str] = []
    events: list[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8 or value is None:
            return
        if isinstance(value, list):
            for item in value[:50]:
                walk(item, depth + 1)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower().replace(" ", "")
                if lowered in ID_KEYS:
                    text = _scalar(item)
                    if text:
                        ids.append(text)
                elif lowered in CD_KEYS:
                    text = _scalar(item)
                    if text:
                        cds.append(text)
                elif lowered in EVENT_KEYS:
                    text = _scalar(item)
                    if text:
                        events.append(text)
                walk(item, depth + 1)
            return
        if isinstance(value, str) and depth == 0:
            for match in ID_RE.finditer(value):
                ids.append(match.group(1))

    walk(payload)
    if raw_text:
        for match in ID_RE.finditer(raw_text):
            ids.append(match.group(1))
        lowered = raw_text.lower()
        for label in (
            "proposal submitted",
            "proposal accepted",
            "proposal rejected",
            "contract created",
            "contract modified",
            "spotline",
            "charge",
        ):
            if label in lowered:
                events.append(label)
    unique_ids = list(dict.fromkeys(item for item in ids if item and item.lower() not in {"id", "contract", "proposal"}))
    unique_cds = list(dict.fromkeys(cds))
    return {
        "ids": unique_ids,
        "contract_cds": unique_cds,
        "event": events[0] if events else "",
        "events": list(dict.fromkeys(events)),
    }


def _scalar(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        if isinstance(value, dict):
            inner = value.get("Value")
            if inner is not None and not isinstance(inner, (dict, list)):
                return str(inner).strip()
            inner = value.get("#text") or value.get("ID") or value.get("Id") or value.get("id")
            if inner is not None and not isinstance(inner, (dict, list)):
                return str(inner).strip()
        return ""
    return str(value).strip()


def message_id_for(raw: bytes, extracted: dict[str, Any]) -> str:
    seed = extracted.get("event") or ""
    ident = ",".join(extracted.get("ids") or extracted.get("contract_cds") or [])
    digest = hashlib.sha256(raw or b"").hexdigest()[:16]
    return f"aquira:{seed}:{ident}:{digest}"


def process_aquira_notification(extracted: dict[str, Any], *, whatif: bool) -> dict[str, Any]:
    from app.sync.orchestrator import SyncContext, SyncOrchestrator

    targets = extracted.get("ids") or extracted.get("contract_cds") or []
    if not targets:
        return {"processed": 0, "reason": "no contract or proposal id in payload"}
    orchestrator = SyncOrchestrator()
    runs: list[dict[str, Any]] = []
    for ident in targets[:5]:
        result = orchestrator.run(
            SyncContext(
                trigger="aquira-webhook",
                whatif=whatif,
                entities=["deals"],
                aquira_id=str(ident),
            )
        )
        runs.append({"aquira_id": ident, "status": result.get("status"), "run_id": result.get("run_id")})
    return {"processed": len(runs), "runs": runs}


def kick_aquira_sync(extracted: dict[str, Any], *, whatif: bool) -> None:
    def _run() -> None:
        try:
            process_aquira_notification(extracted, whatif=whatif)
        except Exception:
            logger.exception("Aquira webhook sync failed")

    threading.Thread(target=_run, daemon=True, name="aquira-webhook-sync").start()
