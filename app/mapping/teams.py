from __future__ import annotations

from typing import Any


def normalize_team_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("-", " ").replace("_", " ").split())


def team_attribute_names(configured: str | None = None) -> list[str]:
    names = [configured or "", "HubSpot Team", "Hubspot Team"]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        text = str(name or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            ordered.append(text)
    return ordered or ["HubSpot Team"]


def team_label_from(entity: dict[str, Any] | None, attribute_names: list[str] | None = None) -> str:
    if not entity:
        return ""
    names = [name.lower() for name in (attribute_names or team_attribute_names())]
    attrs = entity.get("Attributes") or {}
    if isinstance(attrs, dict):
        for key, value in attrs.items():
            if str(key).strip().lower() in names and str(value or "").strip() and not isinstance(value, (dict, list)):
                return str(value).strip()
    elif isinstance(attrs, list):
        from app.aquira.normalize import extract_attributes

        extracted = extract_attributes(entity)
        for key, value in extracted.items():
            if str(key).strip().lower() in names and str(value or "").strip():
                return str(value).strip()
    for key, value in entity.items():
        if str(key).strip().lower() in names and not isinstance(value, (dict, list)) and str(value or "").strip():
            return str(value).strip()
    return ""


def collect_team_keys(catalog: dict[str, list[dict[str, Any]]], attribute_names: list[str] | None = None) -> list[dict[str, Any]]:
    names = attribute_names or team_attribute_names()
    keys: dict[str, dict[str, Any]] = {}

    def add(label: Any, source: str) -> None:
        text = str(label or "").strip()
        if not text:
            return
        key = normalize_team_key(text)
        if not key:
            return
        row = keys.get(key)
        if row is None:
            keys[key] = {"aquira_key": key, "aquira_label": text, "source": source, "count": 1}
            return
        row["count"] += 1
        if source == "attribute":
            row["source"] = "attribute"
            row["aquira_label"] = text

    for client in catalog.get("clients") or []:
        label = team_label_from(client, names)
        if label:
            add(label, "attribute")
        elif client.get("IsAdvertiser"):
            add(client.get("Name"), "advertiser")
    for contract in catalog.get("contracts") or []:
        label = team_label_from(contract, names)
        if label:
            add(label, "attribute")
        for part in str(contract.get("Stations") or "").split(","):
            add(part.strip(), "station")
    return sorted(keys.values(), key=lambda row: (row["source"], row["aquira_label"].lower()))


def suggest_team_map(aquira_keys: list[dict[str, Any]], hubspot_teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {normalize_team_key(team.get("name")): team for team in hubspot_teams if team.get("name")}
    suggestions: list[dict[str, Any]] = []
    for row in aquira_keys:
        key = normalize_team_key(row.get("aquira_key") or row.get("aquira_label"))
        matched = by_name.get(key)
        suggestions.append(
            {
                "aquira_key": key or row.get("aquira_key"),
                "aquira_label": row.get("aquira_label") or row.get("aquira_key"),
                "source": row.get("source") or "attribute",
                "count": int(row.get("count") or 0),
                "hubspot_team_id": str(matched.get("id") or "") if matched else None,
                "hubspot_team_name": matched.get("name") if matched else None,
                "enabled": matched is not None,
                "suggested": matched is not None,
            }
        )
    return suggestions


def resolve_team_id(label: str | None, mapping: dict[str, str], teams_by_name: dict[str, str] | None = None) -> str | None:
    key = normalize_team_key(label)
    if not key:
        return None
    if key in mapping:
        return mapping[key]
    return (teams_by_name or {}).get(key)


def apply_team_ids(
    catalog: dict[str, list[dict[str, Any]]],
    mapping: dict[str, str],
    teams_by_name: dict[str, str] | None = None,
    attribute_names: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    names = attribute_names or team_attribute_names()
    clients = catalog.get("clients") or []
    contracts = catalog.get("contracts") or []
    contacts = catalog.get("contacts") or []
    clients_by_id = {str(client.get("ID")): client for client in clients}

    def assign_from_attribute(entity: dict[str, Any], label: str | None) -> str | None:
        text = str(label or "").strip()
        entity["HubSpotTeam"] = text
        entity["hubspot_team_id"] = resolve_team_id(text, mapping, teams_by_name)
        return entity.get("hubspot_team_id")

    def assign_from_map_only(entity: dict[str, Any], label: str | None) -> str | None:
        text = str(label or "").strip()
        team_id = resolve_team_id(text, mapping, None)
        if team_id:
            entity["HubSpotTeam"] = text
            entity["hubspot_team_id"] = team_id
            return team_id
        entity.setdefault("HubSpotTeam", "")
        entity.setdefault("hubspot_team_id", None)
        return None

    def inherit(entity: dict[str, Any], parent: dict[str, Any] | None) -> str | None:
        if not parent or not parent.get("hubspot_team_id"):
            return None
        entity["HubSpotTeam"] = parent.get("HubSpotTeam") or ""
        entity["hubspot_team_id"] = parent.get("hubspot_team_id")
        return entity.get("hubspot_team_id")

    def parent_first(client: dict[str, Any]) -> tuple[int, str]:
        ident = str(client.get("ID") or "")
        account_id = str(client.get("AccountID") or "")
        if not account_id or account_id == ident:
            return (0, ident)
        return (1, ident)

    for client in sorted(clients, key=parent_first):
        label = team_label_from(client, names)
        if label and assign_from_attribute(client, label):
            continue
        parent = clients_by_id.get(str(client.get("AccountID") or ""))
        if parent is not client and inherit(client, parent):
            continue
        if assign_from_map_only(client, client.get("Name")):
            continue
        assign_from_attribute(client, label)

    for contract in contracts:
        label = team_label_from(contract, names)
        if label and assign_from_attribute(contract, label):
            continue
        inherited = False
        for client_id in (contract.get("AdvertiserID"), contract.get("AccountID")):
            parent = clients_by_id.get(str(client_id or ""))
            if inherit(contract, parent):
                inherited = True
                break
        if inherited:
            continue
        station_ids: dict[str, str] = {}
        for part in str(contract.get("Stations") or "").split(","):
            station = part.strip()
            team_id = resolve_team_id(station, mapping, None)
            if station and team_id:
                station_ids[team_id] = station
        if len(station_ids) == 1:
            team_id, station = next(iter(station_ids.items()))
            contract["HubSpotTeam"] = station
            contract["hubspot_team_id"] = team_id
            continue
        assign_from_attribute(contract, label)

    for contact in contacts:
        label = team_label_from(contact, names)
        if label and assign_from_attribute(contact, label):
            continue
        parent = clients_by_id.get(str(contact.get("ClientID") or ""))
        if inherit(contact, parent):
            continue
        client_id = str(contact.get("ClientID") or "")
        related = [
            contract
            for contract in contracts
            if client_id and client_id in {str(contract.get("AccountID") or ""), str(contract.get("AdvertiserID") or "")} and contract.get("hubspot_team_id")
        ]
        team_ids = {str(contract.get("hubspot_team_id")) for contract in related}
        if len(team_ids) == 1:
            chosen = related[0]
            contact["HubSpotTeam"] = chosen.get("HubSpotTeam") or ""
            contact["hubspot_team_id"] = chosen.get("hubspot_team_id")
            continue
        assign_from_attribute(contact, label)
    return catalog
