from __future__ import annotations

from typing import Any

from app.mapping.owners import _normalize_name

QUALIFIED_SOURCES = {"salesrep", "product"}
SOURCE_RANK = {
    "attribute": 0,
    "salesteam": 1,
    "product": 2,
    "salesrep": 3,
    "station": 4,
    "advertiser": 5,
}


def normalize_team_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("-", " ").replace("_", " ").split())


def team_attribute_names(configured: str | None = None) -> list[str]:
    names = [configured or "", "HubSpot Team", "Hubspot Team", "Hubspot_Team", "HubSpot_Team"]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        text = str(name or "").strip()
        key = normalize_team_key(text)
        if text and key not in seen:
            seen.add(key)
            ordered.append(text)
    return ordered or ["HubSpot Team"]


def _attribute_text(value: Any) -> str:
    inner = value
    if isinstance(inner, dict):
        inner = inner.get("Name") or inner.get("LongName") or inner.get("Text") or inner.get("Label") or inner.get("Value")
    return str(inner or "").strip()


def team_label_from(entity: dict[str, Any] | None, attribute_names: list[str] | None = None) -> str:
    if not entity:
        return ""
    wanted = {normalize_team_key(name) for name in (attribute_names or team_attribute_names())}
    attrs = entity.get("Attributes") or {}
    if isinstance(attrs, dict):
        for key, value in attrs.items():
            if normalize_team_key(key) in wanted:
                text = _attribute_text(value)
                if text:
                    return text
    elif isinstance(attrs, list):
        from app.aquira.normalize import extract_attributes

        extracted = extract_attributes(entity)
        for key, value in extracted.items():
            if normalize_team_key(key) in wanted:
                text = _attribute_text(value)
                if text:
                    return text
    for key, value in entity.items():
        if normalize_team_key(key) in wanted:
            text = _attribute_text(value)
            if text:
                return text
    return ""


def qualified_key(source: str, label: Any) -> str:
    text = normalize_team_key(label)
    if not text:
        return ""
    if source in QUALIFIED_SOURCES:
        return f"{source}:{text}"
    return text


def _rep_label(entity: dict[str, Any], reps_by_id: dict[str, dict[str, Any]]) -> str:
    name = str(entity.get("SalesRepName") or "").strip()
    ident = entity.get("SalesRepID")
    if not name and ident is not None:
        name = str((reps_by_id.get(str(ident)) or {}).get("name") or (reps_by_id.get(str(ident)) or {}).get("Name") or "").strip()
    if name:
        return name
    if ident not in (None, ""):
        return f"Sales rep {ident}"
    return ""


def _sales_teams_for(entity: dict[str, Any], reps_by_id: dict[str, dict[str, Any]]) -> list[str]:
    names = [str(name).strip() for name in (entity.get("SalesTeams") or []) if str(name or "").strip()]
    ident = entity.get("SalesRepID")
    if ident is not None:
        rep = reps_by_id.get(str(ident)) or {}
        names.extend(str(name).strip() for name in (rep.get("SalesTeams") or []) if str(name or "").strip())
        if rep.get("sales_team"):
            names.append(str(rep.get("sales_team")).strip())
    team = str(entity.get("SalesTeam") or "").strip()
    if team:
        names.append(team)
    return list(dict.fromkeys(name for name in names if name))


def _product_names(entity: dict[str, Any]) -> list[str]:
    names = [str(name).strip() for name in (entity.get("ProductNames") or []) if str(name or "").strip()]
    for line in entity.get("lines") or []:
        names.extend(str(name).strip() for name in (line.get("products") or []) if str(name or "").strip())
    product = str(entity.get("Product") or entity.get("ProductName") or "").strip()
    if product:
        names.append(product)
    return list(dict.fromkeys(name for name in names if name))


def collect_team_keys(catalog: dict[str, list[dict[str, Any]]], attribute_names: list[str] | None = None) -> list[dict[str, Any]]:
    names = attribute_names or team_attribute_names()
    keys: dict[str, dict[str, Any]] = {}
    reps_by_id = {str(rep.get("id") or rep.get("ID")): rep for rep in (catalog.get("reps") or [])}

    def add(label: Any, source: str) -> None:
        text = str(label or "").strip()
        if not text:
            return
        key = qualified_key(source, text)
        if not key:
            return
        row = keys.get(key)
        if row is None:
            keys[key] = {"aquira_key": key, "aquira_label": text, "source": source, "count": 1}
            return
        row["count"] += 1
        if SOURCE_RANK.get(source, 9) < SOURCE_RANK.get(row["source"], 9):
            row["source"] = source
            row["aquira_label"] = text

    for client in catalog.get("clients") or []:
        label = team_label_from(client, names)
        if label:
            add(label, "attribute")
        elif client.get("IsAdvertiser"):
            add(client.get("Name"), "advertiser")
        add(_rep_label(client, reps_by_id), "salesrep")
        for team in _sales_teams_for(client, reps_by_id):
            add(team, "salesteam")
    for contract in catalog.get("contracts") or []:
        label = team_label_from(contract, names)
        if label:
            add(label, "attribute")
        for part in str(contract.get("Stations") or "").split(","):
            add(part.strip(), "station")
        add(_rep_label(contract, reps_by_id), "salesrep")
        for team in _sales_teams_for(contract, reps_by_id):
            add(team, "salesteam")
        for product in _product_names(contract):
            add(product, "product")
    for rep in catalog.get("reps") or []:
        add(rep.get("name") or rep.get("Name"), "salesrep")
        for team in _sales_teams_for(rep, {}):
            add(team, "salesteam")
    return sorted(keys.values(), key=lambda row: (SOURCE_RANK.get(row["source"], 9), row["aquira_label"].lower()))


def suggest_team_map(aquira_keys: list[dict[str, Any]], hubspot_teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {normalize_team_key(team.get("name")): team for team in hubspot_teams if team.get("name")}
    suggestions: list[dict[str, Any]] = []
    for row in aquira_keys:
        source = row.get("source") or "attribute"
        key = qualified_key(source, row.get("aquira_label") or row.get("aquira_key")) or str(row.get("aquira_key") or "")
        matched = None
        if source not in QUALIFIED_SOURCES:
            matched = by_name.get(normalize_team_key(row.get("aquira_label") or row.get("aquira_key")))
        suggestions.append(
            {
                "aquira_key": key,
                "aquira_label": row.get("aquira_label") or row.get("aquira_key"),
                "source": source,
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


def resolve_mapped(
    label: Any,
    source: str,
    mapping: dict[str, str],
    teams_by_name: dict[str, str] | None = None,
) -> str | None:
    key = qualified_key(source, label)
    if not key:
        return None
    if key in mapping:
        return mapping[key]
    if source in QUALIFIED_SOURCES:
        return None
    return resolve_team_id(label, mapping, teams_by_name if source in {"attribute", "salesteam"} else None)


def unique_mapped(
    labels: list[Any],
    source: str,
    mapping: dict[str, str],
    teams_by_name: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    found: dict[str, str] = {}
    for label in labels:
        text = str(label or "").strip()
        team_id = resolve_mapped(text, source, mapping, teams_by_name)
        if text and team_id:
            found[team_id] = text
    if len(found) == 1:
        team_id, text = next(iter(found.items()))
        return team_id, text
    return None, ""


def apply_team_ids(
    catalog: dict[str, list[dict[str, Any]]],
    mapping: dict[str, str],
    teams_by_name: dict[str, str] | None = None,
    attribute_names: list[str] | None = None,
    owner_by_aquira: dict[str, str] | None = None,
    owner_team_by_owner_id: dict[str, str] | None = None,
    team_owner_by_team_id: dict[str, str] | None = None,
    owner_by_name: dict[str, str] | None = None,
    teams_by_id: dict[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    names = attribute_names or team_attribute_names()
    clients = catalog.get("clients") or []
    contracts = catalog.get("contracts") or []
    contacts = catalog.get("contacts") or []
    reps_by_id = {str(rep.get("id") or rep.get("ID")): rep for rep in (catalog.get("reps") or [])}
    clients_by_id = {str(client.get("ID")): client for client in clients}
    owner_by_aquira = owner_by_aquira or {}
    owner_team_by_owner_id = owner_team_by_owner_id or {}
    team_owner_by_team_id = team_owner_by_team_id or {}
    owner_by_name = owner_by_name or {}
    teams_by_id = {str(key): str(value) for key, value in (teams_by_id or {}).items() if key and value}

    def set_team(entity: dict[str, Any], team_id: str | None, label: str | None = None) -> str | None:
        if not team_id:
            return None
        official = teams_by_id.get(str(team_id)) or ""
        if not official:
            text = str(label or "").strip()
            if text and teams_by_name and str(teams_by_name.get(normalize_team_key(text)) or "") == str(team_id):
                official = text
            elif text and mapping and str(mapping.get(normalize_team_key(text)) or mapping.get(qualified_key("salesteam", text)) or "") == str(team_id):
                official = text
        entity["hubspot_team_id"] = team_id
        entity["HubSpotTeam"] = official
        return team_id

    def assign_from_attribute(entity: dict[str, Any], label: str | None) -> str | None:
        text = str(label or "").strip()
        return set_team(entity, resolve_team_id(text, mapping, teams_by_name), text)

    def inherit(entity: dict[str, Any], parent: dict[str, Any] | None) -> str | None:
        if not parent or not parent.get("hubspot_team_id"):
            return None
        return set_team(entity, parent.get("hubspot_team_id"), parent.get("HubSpotTeam") or "")

    def assign_unique(entity: dict[str, Any], labels: list[Any], source: str, allow_name_match: bool = False) -> str | None:
        team_id, label = unique_mapped(labels, source, mapping, teams_by_name if allow_name_match else None)
        if team_id:
            return set_team(entity, team_id, label)
        return None

    def assign_from_sales_rep(entity: dict[str, Any]) -> str | None:
        label = _rep_label(entity, reps_by_id)
        ident = entity.get("SalesRepID")
        team_id = resolve_mapped(label, "salesrep", mapping)
        if not team_id and ident is not None:
            team_id = resolve_mapped(f"Sales rep {ident}", "salesrep", mapping)
        if team_id:
            return set_team(entity, team_id)
        owner_id = owner_by_aquira.get(str(ident or ""))
        if owner_id:
            auto = owner_team_by_owner_id.get(str(owner_id))
            if auto:
                return set_team(entity, auto)
        return None

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
        if assign_unique(client, _sales_teams_for(client, reps_by_id), "salesteam", allow_name_match=True):
            continue
        advertiser_team = resolve_mapped(client.get("Name"), "advertiser", mapping)
        if advertiser_team and set_team(client, advertiser_team, str(client.get("Name") or "")):
            continue
        if assign_from_sales_rep(client):
            continue
        assign_from_attribute(client, label)

    for contract in contracts:
        label = team_label_from(contract, names)
        if label and assign_from_attribute(contract, label):
            continue
        inherited = False
        for client_id in (contract.get("AdvertiserID"), contract.get("AccountID")):
            if inherit(contract, clients_by_id.get(str(client_id or ""))):
                inherited = True
                break
        if inherited:
            continue
        if assign_unique(contract, _sales_teams_for(contract, reps_by_id), "salesteam", allow_name_match=True):
            continue
        if assign_unique(contract, _product_names(contract), "product"):
            continue
        stations = [part.strip() for part in str(contract.get("Stations") or "").split(",") if part.strip()]
        if assign_unique(contract, stations, "station"):
            continue
        if assign_from_sales_rep(contract):
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
            if client_id
            and client_id in {str(contract.get("AccountID") or ""), str(contract.get("AdvertiserID") or "")}
            and contract.get("hubspot_team_id")
        ]
        team_ids = {str(contract.get("hubspot_team_id")) for contract in related}
        if len(team_ids) == 1:
            chosen = related[0]
            set_team(contact, chosen.get("hubspot_team_id"), chosen.get("HubSpotTeam") or "")
            continue
        assign_from_attribute(contact, label)

    def assign_owner(entity: dict[str, Any], parent: dict[str, Any] | None = None) -> None:
        if entity.get("hubspot_owner_id"):
            return
        ident = entity.get("SalesRepID")
        mapped = owner_by_aquira.get(str(ident or ""))
        if not mapped:
            mapped = owner_by_name.get(_normalize_name(entity.get("SalesRepName")))
        if mapped:
            entity["hubspot_owner_id"] = mapped
            return
        if parent and parent.get("hubspot_owner_id"):
            entity["hubspot_owner_id"] = parent.get("hubspot_owner_id")
            return
        team_id = entity.get("hubspot_team_id")
        if team_id and team_owner_by_team_id.get(str(team_id)):
            entity["hubspot_owner_id"] = team_owner_by_team_id[str(team_id)]

    for client in sorted(clients, key=parent_first):
        parent = clients_by_id.get(str(client.get("AccountID") or ""))
        assign_owner(client, parent if parent is not client else None)
    for contract in contracts:
        parent = clients_by_id.get(str(contract.get("AdvertiserID") or "")) or clients_by_id.get(
            str(contract.get("AccountID") or "")
        )
        assign_owner(contract, parent)
    for client in clients:
        if client.get("hubspot_owner_id"):
            continue
        client_id = str(client.get("ID") or "")
        related = [
            contract
            for contract in contracts
            if client_id
            and client_id in {str(contract.get("AccountID") or ""), str(contract.get("AdvertiserID") or "")}
            and contract.get("hubspot_owner_id")
        ]
        owners = {str(contract.get("hubspot_owner_id")) for contract in related}
        if len(owners) == 1:
            client["hubspot_owner_id"] = next(iter(owners))
    for contact in contacts:
        assign_owner(contact, clients_by_id.get(str(contact.get("ClientID") or "")))
    return catalog
