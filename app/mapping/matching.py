"""Match incoming Aquira clients to lead companies already created in HubSpot.

The business flow is lead-first: sales creates the company (and contacts) in
HubSpot before anything exists in Aquira. When Aquira later grows the record,
the sync must FIND that company, not create a twin — an unmatched create also
means the same real-world company exists twice with different owners,
associations and revenue.

Policy:
- Only unlinked HubSpot companies are candidates; a company already carrying
  an aquira_id belongs to another Aquira record and is never touched here.
- aquira_id is a unique property: one-to-one on BOTH sides, greedily by score.
  The loser of a contested company always falls to suggestions, never to a
  silent second link.
- Auto-link only on strong, reversible-confidence keys: normalized domain or
  exact legal-suffix-free name. Similar names, phone-only, partial matches are
  SUGGESTIONS — an operator confirms by setting aquira_id on the record (the
  next sync then treats it as linked through the normal path). A wrong link
  poisons attribution and revenue rollups quietly; a duplicate is at least
  visible and mergeable.
"""
from __future__ import annotations

import re
from typing import Any

AUTO_THRESHOLD = 80
SUGGEST_FLOOR = 55

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "lc", "ltd", "limited", "lp", "llp", "plc",
    "corp", "corporation", "co", "company", "gmbh", "sa", "nv", "ag",
}
_SHORT_GENERIC = {"the", "a", "an", "and", "of", "llc", "inc", "co", "group", "media"}


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^[a-z]+://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].strip()
    text = re.sub(r"^www\.", "", text)
    return text if "." in text else ""


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[&]", " and ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [tok for tok in text.split() if tok]
    while tokens and tokens[0] == "the":
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens = tokens[:-1]
    return " ".join(tokens)


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def _meaningful(name_norm: str) -> bool:
    if len(name_norm) < 4:
        return False
    return any(tok not in _SHORT_GENERIC for tok in name_norm.split())


def score_client(client: dict[str, Any], company_props: dict[str, Any]) -> tuple[int, str]:
    """(score, method) for one client × one unlinked HubSpot company."""
    client_domain = normalize_domain(client.get("Website") or client.get("Domain"))
    comp_domain = normalize_domain(company_props.get("domain") or company_props.get("website"))
    if client_domain and comp_domain and client_domain == comp_domain:
        return 100, "domain"

    a = normalize_name(client.get("Name"))
    b = normalize_name(company_props.get("name"))
    phone_match = bool(
        normalize_phone(client.get("Phone"))
        and normalize_phone(client.get("Phone")) == normalize_phone(company_props.get("phone"))
    )
    if a and b and _meaningful(a) and _meaningful(b):
        if a == b:
            return (85, "name+phone") if phone_match else (80, "name")
        if a.startswith(b) or b.startswith(a) or (len(b) >= 6 and b in a) or (len(a) >= 6 and a in b):
            return (70 if phone_match else 60), "similar-name"
    if phone_match:
        return 55, "phone"
    return 0, ""


def match_clients(
    clients: list[dict[str, Any]],
    unlinked_rows: list[dict[str, Any]],
) -> tuple[dict[str, tuple[str, str]], list[dict[str, Any]]]:
    """Returns (links, suggestions).

    links: aquira_id -> (hubspot_id, method), exclusive on both sides.
    suggestions: dicts describing a possible match for an operator to confirm,
    including contests where the winner already auto-linked.
    """
    candidates: list[tuple[int, str, str, str]] = []  # (-score, client_id, hubspot_id, method)
    for client in clients:
        cid = str(client.get("ID") or "")
        if not cid:
            continue
        for row in unlinked_rows:
            hid = str(row.get("id") or row.get("hubspotId") or "")
            if not hid:
                continue
            score, method = score_client(client, row.get("properties") or {})
            if score >= SUGGEST_FLOOR:
                candidates.append((-score, cid, hid, method))

    candidates.sort()
    links: dict[str, tuple[str, str]] = {}
    claimed: set[str] = set()
    suggestions: list[dict[str, Any]] = []
    client_by_id = {str(client.get("ID")): client for client in clients}
    company_name: dict[str, str] = {}
    for row in unlinked_rows:
        hid = str(row.get("id") or row.get("hubspotId") or "")
        company_name[hid] = str((row.get("properties") or {}).get("name") or hid)

    for neg_score, cid, hid, method in candidates:
        score = -neg_score
        if cid in links:
            continue  # client already has its best link
        if hid in claimed:
            suggestions.append(_suggest(client_by_id, company_name, cid, hid, score, method, "company-already-claimed"))
            continue
        if score < AUTO_THRESHOLD:
            suggestions.append(_suggest(client_by_id, company_name, cid, hid, score, method, "below-auto-threshold"))
            continue
        links[cid] = (hid, method)
        claimed.add(hid)

    # Companies that only produced sub-floor candidates for a client are simply
    # absent from both lists — no noise. One suggestion line per (client, company).
    seen: set[tuple[str, str]] = set()
    deduped = []
    for row in suggestions:
        key = (row["aquiraId"], row["hubspotId"])
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    return links, deduped


def _suggest(
    clients_by_id: dict[str, Any],
    company_name: dict[str, str],
    cid: str,
    hid: str,
    score: int,
    method: str,
    reason: str,
) -> dict[str, Any]:
    client = clients_by_id.get(cid) or {}
    return {
        "aquiraId": cid,
        "hubspotId": hid,
        "clientName": str(client.get("Name") or cid),
        "companyName": company_name.get(hid, hid),
        "score": score,
        "method": method,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Rule-based matching — the admin-configured production path.
#
# A rule is (name, AND of field-pair conditions, outcome link|suggest). The
# engine walks rules in PRIORITY order and the FIRST rule that matches an
# Aquira entity decides its outcome outright — a suggest rule placed above a
# link rule deliberately vetoes the auto-link. Exclusivity (one HubSpot record
# per Aquira entity both directions) is enforced here so rule authors cannot
# double-book a company even with sloppy conditions.
# ---------------------------------------------------------------------------
MODES = ("domain", "normalized", "exact", "email", "phone", "contains")


def values_match(mode: str, aquira_value: Any, hubspot_value: Any) -> bool:
    if aquira_value is None or hubspot_value is None:
        return False
    if mode == "domain":
        a = normalize_domain(aquira_value)
        b = normalize_domain(hubspot_value)
        return bool(a and b and a == b)
    if mode == "phone":
        a = normalize_phone(aquira_value)
        b = normalize_phone(hubspot_value)
        return bool(a and a == b)
    if mode == "normalized":
        a = normalize_name(aquira_value)
        b = normalize_name(hubspot_value)
        return bool(a and b and a == b and _meaningful(a) and _meaningful(b))
    if mode == "contains":
        a = normalize_name(aquira_value)
        b = normalize_name(hubspot_value)
        return bool(a and b and len(a) >= 4 and len(b) >= 4 and (a in b or b in a))
    if mode in ("exact", "email"):
        a = str(aquira_value).strip().casefold()
        b = str(hubspot_value).strip().casefold()
        return bool(a and b and a == b)
    return False


def _condition_hit(item: dict[str, Any], props: dict[str, Any], cond: dict[str, Any]) -> bool:
    mode = str(cond.get("mode") or "")
    aq_field = str(cond.get("aquira_field") or "")
    hs_field = str(cond.get("hubspot_field") or "")
    if mode not in MODES or not aq_field or not hs_field:
        return False
    return values_match(mode, item.get(aq_field), props.get(hs_field))


def _entity_label(item: dict[str, Any]) -> str:
    return str(
        item.get("Name")
        or " ".join(p for p in (item.get("FirstName"), item.get("LastName")) if p).strip()
        or item.get("ID")
        or ""
    )


def _candidate_label(props: dict[str, Any], fallback: str) -> str:
    name = str(props.get("name") or "").strip()
    if name:
        return name
    person = " ".join(str(props.get(k) or "").strip() for k in ("firstname", "lastname")).strip()
    return person or str(props.get("email") or "").strip() or fallback


def apply_match_rules(
    items: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    exclusions: set[tuple[str, str]] | None = None,
) -> tuple[dict[str, tuple[str, str]], list[dict[str, Any]]]:
    """exclusions: (aquira_id, hubspot_id) pairs a human already said
    "not a duplicate" to — invisible to every rule, link or suggest."""
    links: dict[str, tuple[str, str]] = {}
    suggestions: list[dict[str, Any]] = []
    claimed: set[str] = set()
    resolved: set[str] = set()
    banned = exclusions or set()
    ordered_items = [i for i in items if str(i.get("ID") or "").strip()]
    candidates: list[tuple[str, dict[str, Any]]] = []
    for row in candidate_rows:
        hid = str(row.get("id") or row.get("hubspotId") or "")
        if hid:
            candidates.append((hid, row.get("properties") or {}))

    for rule in rules:
        conditions = rule.get("conditions") or []
        if not conditions:
            continue
        for item in ordered_items:
            iid = str(item.get("ID"))
            if iid in resolved:
                continue
            for hid, props in candidates:
                if hid in claimed or (iid, hid) in banned:
                    continue
                if not all(_condition_hit(item, props, cond) for cond in conditions):
                    continue
                if str(rule.get("on_match") or "link") == "suggest":
                    suggestions.append(
                        {
                            "aquiraId": iid,
                            "hubspotId": hid,
                            "clientName": _entity_label(item) or iid,
                            "companyName": _candidate_label(props, hid),
                            "score": 0,
                            "method": str(rule.get("name") or "rule"),
                            "reason": f'matched rule "{rule.get("name")}" which only suggests',
                        }
                    )
                    resolved.add(iid)
                    break
                links[iid] = (hid, str(rule.get("name") or "rule"))
                claimed.add(hid)
                resolved.add(iid)
                break
    return links, suggestions
