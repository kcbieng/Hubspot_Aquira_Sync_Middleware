from __future__ import annotations

from datetime import datetime
from typing import Any


def unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "Value" in value:
        return unwrap(value.get("Value"))
    return value


def unwrap_deep(value: Any) -> Any:
    if isinstance(value, list):
        return [unwrap_deep(item) for item in value]
    if isinstance(value, dict):
        if "Value" in value:
            return unwrap_deep(value.get("Value"))
        return {key: unwrap_deep(item) for key, item in value.items()}
    return value


def as_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_array(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any, fallback: str = "") -> str:
    inner = unwrap(value)
    if inner is None:
        return fallback
    return str(inner)


def as_num(value: Any, fallback: float | int = 0) -> float | int:
    inner = unwrap(value)
    if isinstance(inner, bool):
        return int(inner)
    if isinstance(inner, (int, float)):
        return inner
    try:
        number = float(inner)
    except (TypeError, ValueError):
        return fallback
    if number.is_integer():
        return int(number)
    return number


def as_bool(value: Any) -> bool:
    inner = unwrap(value)
    return inner is True or inner == "true" or inner == 1 or inner == "1"


def iso_date(value: Any) -> str:
    raw = as_str(value)
    if not raw:
        return ""
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return raw


def entity_of(payload: Any) -> dict[str, Any]:
    root = as_record(unwrap_deep(payload))
    entity = root.get("Entity")
    if isinstance(entity, dict):
        return entity
    return root


def list_from_envelope(payload: Any) -> list[Any]:
    root = as_record(payload)
    for key in ("Data", "Entities"):
        if isinstance(root.get(key), list):
            return root[key]
    entity = root.get("Entity")
    if isinstance(entity, list):
        return entity
    if isinstance(entity, dict):
        for key in ("Data", "Items", "Results", "Clients", "Contracts", "Users", "Contacts"):
            if isinstance(entity.get(key), list):
                return entity[key]
        if entity.get("ID") is not None or entity.get("Id") is not None:
            return [entity]
    return []


def _split_name(name: str) -> tuple[str, str]:
    parts = [part for part in name.strip().split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def normalize_contact(payload: Any, fallback_client_id: int | None = None) -> dict[str, Any] | None:
    entity = entity_of(payload)
    ident = entity.get("ID", entity.get("Id", entity.get("ContactID", entity.get("Key"))))
    if ident is None or ident == "":
        return None
    name = as_str(entity.get("Name"))
    first_split, last_split = _split_name(name)
    client_id = as_num(entity.get("ClientID", entity.get("AccountID")), fallback_client_id or 0)
    return {
        "ID": ident if isinstance(ident, (int, str)) else as_num(ident),
        "ClientID": int(client_id or 0),
        "FirstName": as_str(entity.get("FirstName")) or first_split or "Unknown",
        "LastName": as_str(entity.get("LastName")) or last_split,
        "Email": as_str(entity.get("Email")).lower() or None,
        "Phone": as_str(entity.get("Phone", entity.get("Mobile", entity.get("WorkPhone")))) or None,
    }


def normalize_client(payload: Any) -> dict[str, Any] | None:
    entity = entity_of(payload)
    ident = as_num(entity.get("ID", entity.get("Id", entity.get("ClientID"))))
    if not ident:
        return None
    client_id = int(ident)
    contacts = [
        row
        for row in (
            normalize_contact(item, client_id)
            for item in as_array(entity.get("Contacts", entity.get("ClientContacts")))
        )
        if row
    ]
    account_id = entity.get("AccountID")
    sales_rep_id = entity.get("SalesRepID")
    return {
        "ID": client_id,
        "Version": int(as_num(entity.get("Version"), 0) or 0) or None,
        "Name": as_str(entity.get("Name", entity.get("LongName", entity.get("ShortName")))) or f"Client {client_id}",
        "LongName": as_str(entity.get("LongName")) or None,
        "ShortName": as_str(entity.get("ShortName")) or None,
        "Email": as_str(entity.get("Email")) or None,
        "Phone": as_str(entity.get("Phone", entity.get("Phone1", entity.get("MainPhone")))) or None,
        "Website": as_str(entity.get("Website", entity.get("Domain", entity.get("URL")))) or None,
        "PhysicalAddress": as_str(entity.get("PhysicalAddress", entity.get("Address1", entity.get("Address")))) or None,
        "City": as_str(entity.get("City")) or None,
        "State": as_str(entity.get("State", entity.get("Region"))) or None,
        "IsAccount": as_bool(entity.get("IsAccount")),
        "IsAdvertiser": as_bool(entity.get("IsAdvertiser")),
        "AccountID": None if account_id in (None, "") else int(as_num(account_id)),
        "SalesRepID": None if sales_rep_id in (None, "") else int(as_num(sales_rep_id)),
        "SalesRepName": as_str(entity.get("SalesRepName")) or None,
        "Contacts": contacts,
    }


def normalize_spot_lines(payload: Any) -> list[dict[str, Any]]:
    root = as_record(unwrap_deep(payload))
    entity = as_record(root.get("Entity"))
    bags = [
        root.get("lines"),
        root.get("Lines"),
        root.get("SpotLines"),
        root.get("Data"),
        entity.get("SpotLines"),
        entity.get("Lines"),
        entity.get("SpotLine"),
    ]
    rows: list[Any] = []
    for bag in bags:
        if isinstance(bag, list) and bag:
            rows.extend(bag)
    lines: list[dict[str, Any]] = []
    for row in rows:
        item = as_record(unwrap_deep(row))
        if not item:
            continue
        station_ref = as_record(item.get("Station"))
        station_id = int(as_num(item.get("station_id", item.get("StationID", station_ref.get("ID")))))
        station = as_str(item.get("station", item.get("StationName", station_ref.get("Name")))) or "ALL"
        start = iso_date(item.get("start", item.get("StartDate", item.get("FlightStart", item.get("DateFrom")))))
        end = iso_date(item.get("end", item.get("EndDate", item.get("FlightEnd", item.get("DateTo")))))
        amount = float(as_num(item.get("amount", item.get("Amount", item.get("NetAmount", item.get("TotalValue"))))))
        spots = as_record(item.get("spots_by_month", item.get("SpotsByMonth")))
        seconds = as_record(item.get("seconds_by_month", item.get("SecondsByMonth")))
        if not start and not end and not amount:
            continue
        lines.append(
            {
                "station_id": station_id,
                "station": station,
                "start": start,
                "end": end,
                "amount": amount,
                "spots_by_month": {key: float(as_num(val)) for key, val in spots.items()} or None,
                "seconds_by_month": {key: float(as_num(val)) for key, val in seconds.items()} or None,
            }
        )
    return lines


def normalize_contract(payload: Any, spot_lines: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    entity = entity_of(payload)
    ident = as_num(entity.get("ID", entity.get("Id", entity.get("ContractID"))))
    if not ident:
        return None
    cancelled = as_bool(entity.get("Cancelled")) or as_bool(entity.get("IsCancelled")) or as_bool(entity.get("Deleted"))
    is_contract = as_bool(entity.get("IsContract"))
    is_proposal = as_bool(entity.get("IsProposal")) or (not is_contract and not cancelled)
    lines = spot_lines if spot_lines else normalize_spot_lines(payload)
    sales_rep_id = entity.get("SalesRepID")
    return {
        "ID": int(ident),
        "ContractCD": as_str(entity.get("ContractCD", entity.get("ContractCode", entity.get("Code")))) or f"C-{ident}",
        "Name": as_str(entity.get("Name", entity.get("AdvertiserName", entity.get("Title")))) or f"Contract {ident}",
        "IsProposal": is_proposal,
        "IsContract": is_contract,
        "Cancelled": cancelled,
        "TotalValue": float(as_num(entity.get("TotalValue", entity.get("Amount", entity.get("NetAmount"))))),
        "StartDate": iso_date(entity.get("StartDate", entity.get("FlightStart"))),
        "EndDate": iso_date(entity.get("EndDate", entity.get("FlightEnd"))),
        "SignDate": iso_date(entity.get("SignDate", entity.get("SignedDate"))) or None,
        "AccountID": int(as_num(entity.get("AccountID", entity.get("ClientID")))),
        "AdvertiserID": int(as_num(entity.get("AdvertiserID", entity.get("AccountID", entity.get("ClientID"))))),
        "SalesRepID": None if sales_rep_id in (None, "") else int(as_num(sales_rep_id)),
        "Status": as_str(entity.get("Status"))
        or ("Cancelled" if cancelled else "Booked" if is_contract else "Proposal"),
        "Stations": as_str(entity.get("Stations", entity.get("StationNames"))) or None,
        "lines": lines,
    }


def normalize_rep(payload: Any) -> dict[str, Any] | None:
    entity = entity_of(payload)
    ident = entity.get("ID", entity.get("Id", entity.get("UserID", entity.get("UserId"))))
    if ident is None or ident == "":
        return None
    first = as_str(entity.get("FirstName"))
    last = as_str(entity.get("LastName"))
    name = as_str(entity.get("Name", entity.get("DisplayName"))) or f"{first} {last}".strip()
    email = as_str(entity.get("Email", entity.get("UserName", entity.get("Username")))).lower()
    return {
        "id": str(ident),
        "ID": ident,
        "name": name or f"User {ident}",
        "Name": name or f"User {ident}",
        "email": email,
        "Email": email,
    }
