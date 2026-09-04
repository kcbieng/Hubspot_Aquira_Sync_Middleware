from __future__ import annotations

from datetime import datetime
from typing import Any


STATUS_LABELS = {0: "Draft", 1: "Proposal", 2: "Booked", 3: "Cancelled"}


def unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "Value" in value:
        return unwrap(value.get("Value"))
    return value


def unwrap_deep(value: Any) -> Any:
    if isinstance(value, list):
        return [unwrap_deep(item) for item in value]
    if isinstance(value, dict):
        # AttributeDataModel is {Name, Value, ID}. Keep the record so extract_attributes
        # can read the name; still unwrap nested FieldValue wrappers inside it.
        if "Value" in value and ("Name" in value or "Label" in value or "AttributeName" in value):
            return {key: unwrap_deep(item) for key, item in value.items()}
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


def _ref_id(value: Any) -> int | None:
    inner = unwrap(value)
    if inner in (None, ""):
        return None
    if isinstance(inner, dict):
        for key in ("SalesRepID", "ID", "Id", "UserID", "ClientID"):
            nested = inner.get(key)
            if nested in (None, ""):
                continue
            if isinstance(nested, dict):
                found = _ref_id(nested)
                if found:
                    return found
                continue
            number = as_num(nested)
            if number:
                return int(number)
        return None
    number = as_num(inner)
    return int(number) if number else None


def _ref_name(value: Any) -> str:
    inner = unwrap(value)
    if isinstance(inner, dict):
        return as_str(inner.get("Name") or inner.get("LongName") or inner.get("DisplayName"))
    return as_str(inner)


def _ref_names(value: Any) -> list[str]:
    inner = unwrap(value)
    items = inner if isinstance(inner, list) else [inner] if inner not in (None, "", []) else []
    names: list[str] = []
    for item in items:
        rec = as_record(unwrap(item))
        nested = rec.get("Value")
        if isinstance(nested, dict) and rec.get("Name") in (None, ""):
            rec = as_record(nested)
        name = as_str(rec.get("Name") or rec.get("LongName") or rec.get("ShortName") or rec.get("DisplayName")).strip()
        if not name:
            name = _ref_name(item).strip()
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


def _sales_rep_id(entity: dict[str, Any]) -> int | None:
    direct = _ref_id(entity.get("SalesRepID") or entity.get("SalesRep"))
    if direct:
        return direct
    reps = unwrap(entity.get("SalesReps"))
    if not isinstance(reps, list):
        return None
    rows = [as_record(unwrap(row)) for row in reps]
    selected = [row for row in rows if as_bool(row.get("Selected"))]
    for row in selected or rows:
        ident = _ref_id(row.get("SalesRepID") or row.get("ID") or row)
        if ident:
            return ident
    return None


def _sales_rep_name(entity: dict[str, Any]) -> str:
    direct = _entity_str(entity, "SalesRepName") or _ref_name(entity.get("SalesRep"))
    if direct:
        return direct
    reps = unwrap(entity.get("SalesReps"))
    if not isinstance(reps, list):
        return ""
    rows = [as_record(unwrap(row)) for row in reps]
    selected = [row for row in rows if as_bool(row.get("Selected"))]
    for row in selected or rows:
        name = _ref_name(row.get("SalesRepID") or row.get("SalesRep") or row)
        if name:
            return name
    return ""


def _sales_teams(entity: dict[str, Any]) -> list[str]:
    names = _ref_names(entity.get("SalesTeams") or entity.get("SalesTeam") or entity.get("SalesTeamItem"))
    reps = unwrap(entity.get("SalesReps"))
    if isinstance(reps, list):
        rows = [as_record(unwrap(row)) for row in reps]
        selected = [row for row in rows if as_bool(row.get("Selected"))]
        for row in selected or rows:
            names.extend(_ref_names(row.get("SalesTeam") or row.get("SalesTeamItem")))
            names.extend(_ref_names(as_record(unwrap(row.get("SalesRepID"))).get("SalesTeam")))
    return list(dict.fromkeys(name for name in names if name))


def _status_int(value: Any) -> int | None:
    inner = unwrap(value)
    if inner in (None, ""):
        return None
    if isinstance(inner, bool):
        return None
    if isinstance(inner, dict):
        for key in ("ID", "Id", "Value", "Status"):
            nested = inner.get(key)
            if nested in (None, "", inner):
                continue
            found = _status_int(nested)
            if found is not None:
                return found
        return None
    try:
        return int(float(inner))
    except (TypeError, ValueError):
        label = str(inner).strip().lower()
        return {"draft": 0, "proposal": 1, "booked": 2, "contract": 2, "cancelled": 3, "canceled": 3}.get(label)


def _first_station_name(value: Any) -> str:
    inner = unwrap(value)
    if isinstance(inner, list):
        names = [_ref_name(item) or as_str(item) for item in inner]
        return ", ".join(name for name in names if name)
    if isinstance(inner, dict):
        return _ref_name(inner)
    return as_str(inner)


def _entity_get(entity: dict[str, Any], *names: str) -> Any:
    wanted = {name.lower() for name in names}
    for key, value in entity.items():
        if str(key).lower() in wanted and value not in (None, ""):
            return value
    return None


def _entity_str(entity: dict[str, Any], *names: str) -> str:
    wanted = [name.lower() for name in names]
    lower_map = {str(key).lower(): value for key, value in entity.items()}
    for name in wanted:
        text = as_str(lower_map.get(name)).strip()
        if text:
            return text
    return ""


def _address_parts(entity: dict[str, Any]) -> tuple[str, str, str]:
    physical = _entity_get(entity, "PhysicalAddress", "Addresses", "Address")
    if isinstance(physical, dict) and physical.get("Physical") is not None:
        physical = physical.get("Physical")
    if isinstance(physical, dict):
        street = as_str(
            physical.get("Address")
            or physical.get("Value")
            or physical.get("Line1")
            or physical.get("Address1")
        ).strip()
        city = as_str(physical.get("City")).strip()
        state = as_str(physical.get("Region") or physical.get("State")).strip()
        return street, city, state
    street = as_str(physical) if not isinstance(physical, dict) else ""
    if not street:
        street = _entity_str(entity, "Address1", "Address")
    return street, _entity_str(entity, "City"), _entity_str(entity, "State", "Region")


def _party_flags(entity: dict[str, Any]) -> tuple[bool, bool]:
    is_account = as_bool(_entity_get(entity, "IsAccount"))
    is_advertiser = as_bool(_entity_get(entity, "IsAdvertiser"))
    if is_account or is_advertiser:
        return is_account, is_advertiser
    type_code = _status_int(_entity_get(entity, "Type", "ClientType"))
    if type_code:
        return bool(type_code & 1), bool(type_code & 2)
    return False, False


def extract_attributes(entity: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}

    def add(name: Any, value: Any) -> None:
        label = as_str(name or "").strip()
        inner = unwrap(value)
        if isinstance(inner, dict):
            inner = inner.get("Name") or inner.get("Text") or inner.get("Label") or inner.get("Value")
        text = as_str(inner).strip()
        if label and text and not isinstance(inner, (dict, list)):
            out[label] = text

    bags = [
        entity.get("Attributes"),
        entity.get("ClientAttributes"),
        entity.get("UserAttributes"),
        entity.get("ContractAttributes"),
        entity.get("CustomAttributes"),
    ]
    for raw in bags:
        if not raw:
            continue
        if isinstance(raw, dict):
            if raw.get("Name") or raw.get("Label") or raw.get("AttributeName"):
                raw = [raw]
            else:
                for key, value in raw.items():
                    add(key, value)
                continue
        items = raw if isinstance(raw, list) else as_array(unwrap(raw))
        for item in items:
            rec = as_record(item)
            if rec.get("Name") in (None, "") and isinstance(rec.get("Value"), dict) and rec["Value"].get("Name"):
                rec = as_record(rec.get("Value"))
            add(rec.get("Name") or rec.get("Label") or rec.get("AttributeName"), rec.get("Value"))
    return out


def normalize_contact(payload: Any, fallback_client_id: int | None = None) -> dict[str, Any] | None:
    entity = entity_of(payload)
    ident = _entity_get(entity, "ID", "Id", "ContactID", "Key")
    if ident in (None, ""):
        return None
    name = _entity_str(entity, "Name", "LongName")
    first_split, last_split = _split_name(name)
    client_id = as_num(
        entity.get("ClientID") or entity.get("AccountID") or _ref_id(entity.get("Client")),
        fallback_client_id or 0,
    )
    email = (
        _entity_str(entity, "Email", "EmailAddress")
        or as_str(entity.get("Email")).lower()
        or as_str(entity.get("EmailAddress")).lower()
    ).lower() or None
    phone = (
        _entity_str(
            entity,
            "Phone",
            "BusinessPhone1",
            "PersonalDirectDialPhone",
            "PersonalMobilePhone",
            "BusinessMobilePhone",
            "Mobile",
            "WorkPhone",
        )
        or None
    )
    return {
        "ID": ident if isinstance(ident, (int, str)) else as_num(ident),
        "ClientID": int(client_id or 0),
        "FirstName": _entity_str(entity, "FirstName") or first_split or "Unknown",
        "LastName": _entity_str(entity, "LastName") or last_split,
        "Email": email,
        "Phone": phone,
        "Attributes": extract_attributes(entity),
    }


def normalize_client(payload: Any) -> dict[str, Any] | None:
    entity = entity_of(payload)
    ident = as_num(_entity_get(entity, "ID", "Id", "ClientID") or entity.get("ID") or entity.get("Id") or entity.get("ClientID"))
    if not ident:
        return None
    client_id = int(ident)
    contacts = [
        row
        for row in (
            normalize_contact(item, client_id)
            for item in as_array(_entity_get(entity, "Contacts", "ClientContacts") or entity.get("Contacts") or entity.get("ClientContacts") or [])
        )
        if row
    ]
    account_id = _ref_id(_entity_get(entity, "AccountID", "Account"))
    sales_rep_id = _sales_rep_id(entity)
    is_account, is_advertiser = _party_flags(entity)
    name = _entity_str(entity, "Name", "Fullname", "FullName", "LongName", "Shortname", "ShortName", "ClientCD") or f"Client {client_id}"
    street, city, state = _address_parts(entity)
    return {
        "ID": client_id,
        "Version": int(as_num(entity.get("Version"), 0) or 0) or None,
        "Name": name,
        "LongName": _entity_str(entity, "LongName") or None,
        "ShortName": _entity_str(entity, "Shortname", "ShortName") or None,
        "Email": _entity_str(entity, "Email") or None,
        "Phone": _entity_str(entity, "Phone", "BusinessPhone1", "Phone1", "MainPhone") or None,
        "Website": _entity_str(entity, "Website", "Domain", "URL") or None,
        "PhysicalAddress": street or None,
        "City": city or None,
        "State": state or None,
        "IsAccount": is_account,
        "IsAdvertiser": is_advertiser,
        "AccountID": account_id,
        "SalesRepID": sales_rep_id,
        "SalesRepName": _sales_rep_name(entity) or _entity_str(entity, "SalesRepName") or _ref_name(entity.get("SalesRep")) or None,
        "SalesTeams": _sales_teams(entity),
        "Contacts": contacts,
        "Attributes": extract_attributes(entity),
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
        entity.get("SpotLinesSummarized"),
        as_record(entity.get("Summary")).get("SpotLines"),
    ]
    rows: list[Any] = []
    for bag in bags:
        if isinstance(bag, list) and bag:
            rows.extend(bag)
            break
    lines: list[dict[str, Any]] = []
    for row in rows:
        item = as_record(unwrap_deep(row))
        if not item:
            continue
        value = item.get("Value")
        if isinstance(value, dict) and not item.get("StartDate") and not item.get("FirstSpot"):
            item = as_record(unwrap_deep(value))
        stations = item.get("SelectedStationsCombined") or item.get("Stations") or item.get("Station")
        station_inner = unwrap(stations)
        station_ref = as_record(item.get("Station") if isinstance(item.get("Station"), dict) else {})
        if isinstance(station_inner, dict):
            station_ref = as_record(station_inner)
        elif isinstance(station_inner, list) and station_inner:
            station_ref = as_record(unwrap(station_inner[0]))
        station_id = int(as_num(item.get("station_id", item.get("StationID", station_ref.get("ID")))))
        station = (
            as_str(item.get("station", item.get("StationName", station_ref.get("Name"))))
            or _first_station_name(stations)
            or "ALL"
        )
        start = iso_date(
            item.get("start")
            or item.get("StartDate")
            or item.get("FlightStart")
            or item.get("DateFrom")
            or item.get("FirstSpot")
        )
        end = iso_date(
            item.get("end")
            or item.get("EndDate")
            or item.get("FlightEnd")
            or item.get("DateTo")
            or item.get("LastSpot")
        )
        amount = float(
            as_num(
                item.get("amount")
                or item.get("Amount")
                or item.get("NetAmount")
                or item.get("TotalValue")
                or item.get("BookedTotalAmount")
            )
        )
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
                "products": _ref_names(item.get("Products") or item.get("Product")),
                "spots_by_month": {key: float(as_num(val)) for key, val in spots.items()} or None,
                "seconds_by_month": {key: float(as_num(val)) for key, val in seconds.items()} or None,
            }
        )
    if not lines:
        summary = as_record(entity.get("Summary") or root.get("Summary"))
        start = iso_date(summary.get("StartDate"))
        end = iso_date(summary.get("EndDate"))
        amount = float(as_num(summary.get("NetAmount") or summary.get("GrossAmount") or summary.get("Amount")))
        if start or end or amount:
            lines.append(
                {
                    "station_id": 0,
                    "station": "ALL",
                    "start": start,
                    "end": end,
                    "amount": amount,
                    "products": [],
                    "spots_by_month": None,
                    "seconds_by_month": None,
                }
            )
    return lines


def normalize_contract(payload: Any, spot_lines: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    entity = entity_of(payload)
    ident = as_num(entity.get("ID", entity.get("Id", entity.get("ContractID"))))
    if not ident:
        return None
    summary = as_record(entity.get("Summary"))
    cancelled = as_bool(entity.get("Cancelled")) or as_bool(entity.get("IsCancelled")) or as_bool(entity.get("Deleted"))
    is_contract = as_bool(entity.get("IsContract"))
    is_proposal = as_bool(entity.get("IsProposal"))
    status_code = _status_int(entity.get("Status"))
    if not is_contract and not is_proposal and not cancelled and status_code is not None:
        if status_code == 3:
            cancelled = True
        elif status_code == 2:
            is_contract = True
        elif status_code in (0, 1):
            is_proposal = True
    if not is_proposal:
        is_proposal = not is_contract and not cancelled
    lines = spot_lines if spot_lines is not None else normalize_spot_lines(payload)
    advertiser_id = _ref_id(entity.get("AdvertiserID") or entity.get("Advertiser"))
    account_id = _ref_id(entity.get("AccountID") or entity.get("Account") or entity.get("ClientID"))
    if not advertiser_id:
        advertiser_id = account_id
    sales_rep_id = _sales_rep_id(entity)
    advertiser_name = _ref_name(entity.get("Advertiser")) or as_str(entity.get("AdvertiserName"))
    account_name = _ref_name(entity.get("Account")) or as_str(entity.get("AccountName"))
    contract_cd = as_str(entity.get("ContractCD", entity.get("ContractCode", entity.get("Code")))) or f"C-{ident}"
    description = as_str(entity.get("Description")).strip() or None
    raw_name = as_str(entity.get("Name", entity.get("Title")))
    if description:
        display_name = description
    elif advertiser_name:
        display_name = advertiser_name
    elif raw_name and raw_name not in {str(int(ident)), contract_cd}:
        display_name = raw_name
    else:
        display_name = f"{'Proposal' if is_proposal and not is_contract else 'Contract'} {int(ident)}"
    total = float(
        as_num(
            entity.get("TotalValue")
            or entity.get("Amount")
            or entity.get("NetAmount")
            or entity.get("GrossAmount")
            or summary.get("NetAmount")
            or summary.get("GrossAmount")
        )
    )
    if not total and lines:
        total = float(sum(float(line.get("amount") or 0) for line in lines))
    start = iso_date(entity.get("StartDate") or entity.get("FlightStart") or summary.get("StartDate"))
    end = iso_date(entity.get("EndDate") or entity.get("FlightEnd") or summary.get("EndDate"))
    if not start and lines:
        start = min((line.get("start") or "" for line in lines if line.get("start")), default="")
    if not end and lines:
        end = max((line.get("end") or "" for line in lines if line.get("end")), default="")
    stations = as_str(entity.get("Stations") or entity.get("StationNames")) or None
    if not stations and lines:
        unique = list(
            dict.fromkeys(
                str(line.get("station") or "")
                for line in lines
                if line.get("station") and line.get("station") != "ALL"
            )
        )
        stations = ", ".join(unique) or None
    products = list(
        dict.fromkeys(
            [
                *_ref_names(entity.get("Product") or entity.get("Products")),
                *(name for line in lines for name in (line.get("products") or [])),
            ]
        )
    )
    if cancelled:
        status_label = "Cancelled"
    elif is_contract:
        status_label = "Booked"
    elif is_proposal:
        status_label = "Proposal"
    else:
        status_label = STATUS_LABELS.get(status_code or -1, as_str(entity.get("Status")) or "Proposal")
    return {
        "ID": int(ident),
        "ContractCD": contract_cd,
        "Name": display_name,
        "Description": description,
        "IsProposal": is_proposal,
        "IsContract": is_contract,
        "Cancelled": cancelled,
        "TotalValue": total,
        "StartDate": start,
        "EndDate": end,
        "SignDate": iso_date(entity.get("SignDate") or entity.get("SignedDate")) or None,
        "AccountID": account_id,
        "AdvertiserID": advertiser_id,
        "AccountName": account_name or None,
        "AdvertiserName": advertiser_name or None,
        "SalesRepID": sales_rep_id,
        "SalesRepName": _sales_rep_name(entity) or None,
        "SalesTeams": _sales_teams(entity),
        "Status": status_label,
        "Stations": stations,
        "ProductNames": products,
        "lines": lines,
        "Attributes": extract_attributes(entity),
    }


def merge_client(summary: dict[str, Any] | None, loaded: dict[str, Any] | None) -> dict[str, Any] | None:
    if not loaded and not summary:
        return None
    if not loaded:
        return summary
    if not summary:
        return loaded

    def _empty(value: Any) -> bool:
        return value in (None, "", [], 0, 0.0)

    merged = dict(summary)
    ident = merged.get("ID") or loaded.get("ID")
    for key, value in loaded.items():
        if key in {"IsAccount", "IsAdvertiser"}:
            merged[key] = bool(value or merged.get(key))
            continue
        if key == "Contacts":
            if value:
                merged[key] = value
            continue
        if key == "SalesTeams":
            merged[key] = list(dict.fromkeys([*(merged.get("SalesTeams") or []), *(value or [])]))
            continue
        if key == "Attributes":
            merged[key] = {**(merged.get("Attributes") or {}), **(value or {})}
            continue
        if key == "Name" and _is_fallback_client_name(value, ident) and not _is_fallback_client_name(merged.get("Name"), ident):
            continue
        if not _empty(value):
            merged[key] = value
    return merged


def _is_fallback_client_name(name: Any, ident: Any) -> bool:
    text = str(name or "").strip()
    return not text or text == f"Client {ident}" or text == str(ident)


def clients_from_contracts(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for contract in contracts:
        account_id = contract.get("AccountID")
        advertiser_id = contract.get("AdvertiserID")
        if account_id:
            ident = int(account_id)
            row = by_id.setdefault(
                ident,
                {"ID": ident, "Name": f"Client {ident}", "IsAccount": False, "IsAdvertiser": False, "Contacts": []},
            )
            if contract.get("AccountName"):
                row["Name"] = str(contract.get("AccountName"))
            row["IsAccount"] = True
            if advertiser_id and int(advertiser_id) == ident:
                row["IsAdvertiser"] = True
        if advertiser_id:
            ident = int(advertiser_id)
            row = by_id.setdefault(
                ident,
                {"ID": ident, "Name": f"Client {ident}", "IsAccount": False, "IsAdvertiser": False, "Contacts": []},
            )
            advertiser_name = contract.get("AdvertiserName") or (
                None if str(contract.get("Name") or "").startswith("Contract ") else contract.get("Name")
            )
            if advertiser_name and _is_fallback_client_name(row.get("Name"), ident):
                row["Name"] = str(advertiser_name)
            row["IsAdvertiser"] = True
            if account_id and int(account_id) == ident:
                row["IsAccount"] = True
    return list(by_id.values())


def merge_contract(summary: dict[str, Any] | None, loaded: dict[str, Any] | None) -> dict[str, Any] | None:
    if not loaded and not summary:
        return None
    if not loaded:
        return summary
    if not summary:
        return loaded

    def _empty(value: Any) -> bool:
        return value in (None, "", 0, 0.0) or value == []

    merged = dict(summary)
    for key, value in loaded.items():
        if key in {"IsContract", "IsProposal", "Cancelled"}:
            merged[key] = value
            continue
        if key == "lines":
            if value:
                merged[key] = value
            continue
        if key == "Attributes":
            merged[key] = {**(merged.get("Attributes") or {}), **(value or {})}
            continue
        if key in {"SalesTeams", "ProductNames"}:
            if value:
                merged[key] = value
            continue
        if not _empty(value):
            merged[key] = value
    return merged


def normalize_rep(payload: Any) -> dict[str, Any] | None:
    entity = entity_of(payload)
    ident = entity.get("ID", entity.get("Id", entity.get("UserID", entity.get("UserId"))))
    if ident is None or ident == "":
        return None
    first = as_str(entity.get("FirstName"))
    last = as_str(entity.get("LastName"))
    name = as_str(entity.get("Name", entity.get("DisplayName"))) or f"{first} {last}".strip()
    email = as_str(entity.get("Email", entity.get("UserName", entity.get("Username")))).lower()
    teams = _sales_teams(entity)
    return {
        "id": str(ident),
        "ID": ident,
        "name": name or f"User {ident}",
        "Name": name or f"User {ident}",
        "email": email,
        "Email": email,
        "SalesTeams": teams,
        "sales_team": teams[0] if teams else None,
    }
