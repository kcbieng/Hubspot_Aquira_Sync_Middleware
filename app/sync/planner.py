from __future__ import annotations

from typing import Any

from app.aquira.fieldvalues import unwrap
from app.hashutil import content_hash
from app.mapping.parties import party_type_for_client
from app.mapping.revenue import allocate_revenue

IDENTITY_COMPANY_FIELDS = ("name", "phone", "domain", "address", "city", "state")
IDENTITY_CONTACT_FIELDS = ("firstname", "lastname", "email", "phone")


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return None


def _same(left: Any, right: Any) -> bool:
    if left is right:
        return True
    left = unwrap(left)
    right = unwrap(right)
    if left is None and right in (None, ""):
        return True
    if right is None and left in (None, ""):
        return True
    left_bool = _as_bool(left)
    right_bool = _as_bool(right)
    if left_bool is not None and right_bool is not None:
        return left_bool is right_bool
    try:
        if left not in (None, "") and right not in (None, ""):
            if float(left) == float(right):
                return True
    except (TypeError, ValueError):
        pass
    if left is None or right is None:
        return left == right
    return str(left).strip() == str(right).strip()


def field_diff(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[dict[str, Any]]:
    old_map = old or {}
    new_map = new or {}
    changes: list[dict[str, Any]] = []
    for key in sorted(new_map):
        current = unwrap(old_map.get(key))
        proposed = unwrap(new_map.get(key))
        if not _same(current, proposed):
            changes.append({"field": key, "from": current, "to": proposed})
    return changes


def company_properties(client: dict[str, Any]) -> dict[str, Any]:
    website = str(client.get("Website") or "")
    domain = website.replace("https://", "").replace("http://", "")
    return {
        "name": client.get("Name") or "",
        "domain": domain,
        "phone": client.get("Phone") or "",
        "address": client.get("PhysicalAddress") or "",
        "city": client.get("City") or "",
        "state": client.get("State") or "",
        "aquira_id": str(client.get("ID")),
        "aquira_party_type": party_type_for_client(client),
        "aquira_version": client.get("Version"),
    }


def contact_properties(contact: dict[str, Any]) -> dict[str, Any]:
    return {
        "firstname": contact.get("FirstName") or "",
        "lastname": contact.get("LastName") or "",
        "email": str(contact.get("Email") or "").lower(),
        "phone": contact.get("Phone") or "",
        "aquira_id": str(contact.get("ID")),
        "aquira_entity_type": "contact",
        "aquira_client_id": str(contact.get("ClientID") or ""),
    }


def deal_properties(contract: dict[str, Any], advertiser_name: str | None = None) -> dict[str, Any]:
    advertiser = advertiser_name or contract.get("Name") or "Contract"
    cancelled = bool(contract.get("Cancelled"))
    is_contract = bool(contract.get("IsContract"))
    stage = "closedlost" if cancelled else "closedwon" if is_contract else "proposal"
    return {
        "dealname": f"{contract.get('ContractCD')} — {advertiser}",
        "amount": contract.get("TotalValue") or 0,
        "closedate": contract.get("EndDate") or "",
        "pipeline": "default",
        "dealstage": stage,
        "aquira_id": str(contract.get("ID")),
        "aquira_contract_cd": contract.get("ContractCD") or "",
        "aquira_status": contract.get("Status") or ("Booked" if is_contract else "Proposal"),
        "aquira_is_proposal": bool(contract.get("IsProposal")),
        "aquira_is_contract": is_contract,
        "aquira_sign_date": contract.get("SignDate") or "",
        "aquira_start_date": contract.get("StartDate") or "",
        "aquira_end_date": contract.get("EndDate") or "",
        "aquira_stations": contract.get("Stations") or "KCBI",
        "aquira_account_id": str(contract.get("AccountID") or ""),
        "aquira_advertiser_id": str(contract.get("AdvertiserID") or ""),
        "aquira_sales_rep": str(contract.get("SalesRepID") or ""),
    }


def plan_upsert(
    entity_type: str,
    aquira_id: str,
    name: str,
    proposed: dict[str, Any],
    existing: dict[str, Any] | None,
    associations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    digest = content_hash(proposed)
    existing_hash = (existing or {}).get("hash")
    diffs = [
        row
        for row in field_diff((existing or {}).get("properties") or {}, proposed)
        if row["field"] != "aquira_version"
    ]
    unchanged = bool(existing) and (existing_hash == digest or not diffs)
    if unchanged:
        return {
            "entityType": entity_type,
            "aquiraId": aquira_id,
            "hubspotId": existing.get("hubspotId") if existing else None,
            "action": "skip",
            "name": name,
            "diffs": [],
            "properties": proposed,
            "associations": associations,
        }
    if existing is None:
        diffs = [{**row, "from": None} for row in diffs]
    return {
        "entityType": entity_type,
        "aquiraId": aquira_id,
        "hubspotId": (existing or {}).get("hubspotId"),
        "action": "update" if existing else "create",
        "name": name,
        "diffs": diffs,
        "properties": proposed,
        "associations": associations,
    }


def _company_sort_key(client: dict[str, Any]) -> tuple[int, str]:
    if client.get("IsAccount"):
        return (0, str(client.get("Name") or ""))
    if client.get("IsAdvertiser"):
        return (2, str(client.get("Name") or ""))
    return (1, str(client.get("Name") or ""))


def plan_companies(clients: list[dict[str, Any]], existing_by_aquira: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for client in sorted(clients, key=_company_sort_key):
        from_aquira = company_properties(client)
        existing = existing_by_aquira.get(str(client.get("ID")))
        props = dict(from_aquira)
        if existing:
            current = existing.get("properties") or {}
            for field in IDENTITY_COMPANY_FIELDS:
                if current.get(field) not in (None, ""):
                    props[field] = current.get(field)
        account_id = None
        if client.get("IsAdvertiser") and client.get("AccountID") and client.get("AccountID") != client.get("ID"):
            account_id = str(client.get("AccountID"))
        items.append(
            plan_upsert(
                "company",
                str(client.get("ID")),
                str(props.get("name") or ""),
                props,
                existing,
                {"parentCompanyId": account_id},
            )
        )
    return items


def plan_contacts(
    contacts: list[dict[str, Any]],
    existing_by_aquira: dict[str, dict[str, Any]],
    existing_by_email: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for contact in contacts:
        from_aquira = contact_properties(contact)
        email = str(from_aquira.get("email") or "")
        existing = existing_by_aquira.get(str(contact.get("ID"))) or (existing_by_email.get(email) if email else None)
        props = dict(from_aquira)
        if existing:
            current = existing.get("properties") or {}
            for field in IDENTITY_CONTACT_FIELDS:
                if current.get(field) not in (None, ""):
                    props[field] = current.get(field)
            if props.get("email"):
                props["email"] = str(props["email"]).lower()
        items.append(
            plan_upsert(
                "contact",
                str(contact.get("ID")),
                f"{props.get('firstname')} {props.get('lastname')}".strip(),
                props,
                existing,
                {"companyIds": [str(contact.get("ClientID"))]},
            )
        )
    return items


def plan_deals(
    contracts: list[dict[str, Any]],
    existing_by_aquira: dict[str, dict[str, Any]],
    owner_by_aquira_user: dict[str, str],
    client_name_by_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    names = client_name_by_id or {}
    for contract in contracts:
        advertiser_name = names.get(str(contract.get("AdvertiserID")))
        props = deal_properties(contract, advertiser_name)
        owner_id = None
        if contract.get("SalesRepID"):
            owner_id = owner_by_aquira_user.get(str(contract.get("SalesRepID")))
        if owner_id:
            props["hubspot_owner_id"] = owner_id
        company_ids = list({str(contract.get("AccountID") or ""), str(contract.get("AdvertiserID") or "")} - {""})
        items.append(
            plan_upsert(
                "deal",
                str(contract.get("ID")),
                str(props.get("dealname") or ""),
                props,
                existing_by_aquira.get(str(contract.get("ID"))),
                {"companyIds": company_ids, "ownerId": owner_id},
            )
        )
    return items


def plan_revenue(contracts: list[dict[str, Any]], existing_by_aquira: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    produced: set[str] = set()
    for contract in contracts:
        periods = allocate_revenue(
            {
                "contract_id": contract.get("ID"),
                "contract_cd": contract.get("ContractCD"),
                "kind": "booked" if contract.get("IsContract") else "proposal",
                "fallback_start": contract.get("StartDate"),
                "fallback_end": contract.get("EndDate"),
                "fallback_amount": contract.get("TotalValue"),
                "lines": contract.get("lines") or [],
            }
        )
        for period in periods:
            produced.add(period["aquira_id"])
            props = {
                "aquira_id": period["aquira_id"],
                "period": period["period"],
                "amount": period["amount"],
                "station": period["station"],
                "station_id": period["station_id"],
                "kind": period["kind"],
                "contract_cd": period["contract_cd"],
            }
            items.append(
                plan_upsert(
                    "revenue_period",
                    period["aquira_id"],
                    f"{period['contract_cd']} {period['period']} {period['station']}",
                    props,
                    existing_by_aquira.get(period["aquira_id"]),
                    {
                        "dealId": str(contract.get("ID")),
                        "companyIds": [str(contract.get("AccountID") or ""), str(contract.get("AdvertiserID") or "")],
                    },
                )
            )
    for aquira_id, existing in existing_by_aquira.items():
        if aquira_id in produced:
            continue
        items.append(
            {
                "entityType": "revenue_period",
                "aquiraId": aquira_id,
                "hubspotId": existing.get("hubspotId"),
                "action": "delete-stale",
                "name": str((existing.get("properties") or {}).get("name") or aquira_id),
                "diffs": [{"field": "amount", "from": (existing.get("properties") or {}).get("amount"), "to": None}],
                "properties": {},
            }
        )
    return items


def plan_identity_writebacks(hubspot_companies: list[dict[str, Any]], aquira_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for company in hubspot_companies:
        client = aquira_by_id.get(str(company.get("aquira_id") or ""))
        if not client:
            continue
        properties = company.get("properties") or {}
        proposed = {
            "Name": str(properties.get("name") or client.get("Name") or ""),
            "Phone": str(properties.get("phone") or client.get("Phone") or ""),
            "Website": str(properties.get("domain") or client.get("Website") or ""),
            "PhysicalAddress": str(properties.get("address") or client.get("PhysicalAddress") or ""),
        }
        current = {
            "Name": client.get("Name") or "",
            "Phone": client.get("Phone") or "",
            "Website": client.get("Website") or "",
            "PhysicalAddress": client.get("PhysicalAddress") or "",
        }
        diffs = field_diff(current, proposed)
        if not diffs:
            continue
        items.append(
            {
                "entityType": "client",
                "aquiraId": str(company.get("aquira_id")),
                "hubspotId": company.get("hubspotId"),
                "action": "update",
                "name": client.get("Name") or "",
                "diffs": diffs,
                "properties": proposed,
                "writeback": True,
            }
        )
    return items


def plan_contact_writebacks(hubspot_contacts: list[dict[str, Any]], aquira_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in hubspot_contacts:
        contact = aquira_by_id.get(str(row.get("aquira_id") or ""))
        if not contact:
            continue
        properties = row.get("properties") or {}
        proposed = {
            "FirstName": str(properties.get("firstname") or contact.get("FirstName") or ""),
            "LastName": str(properties.get("lastname") or contact.get("LastName") or ""),
            "Email": str(properties.get("email") or contact.get("Email") or ""),
            "Phone": str(properties.get("phone") or contact.get("Phone") or ""),
        }
        current = {
            "FirstName": contact.get("FirstName") or "",
            "LastName": contact.get("LastName") or "",
            "Email": contact.get("Email") or "",
            "Phone": contact.get("Phone") or "",
        }
        diffs = field_diff(current, proposed)
        if not diffs:
            continue
        items.append(
            {
                "entityType": "contact",
                "aquiraId": str(row.get("aquira_id")),
                "hubspotId": row.get("hubspotId"),
                "action": "update",
                "name": f"{contact.get('FirstName')} {contact.get('LastName')}".strip(),
                "diffs": diffs,
                "properties": proposed,
                "writeback": True,
                "associations": {"clientId": contact.get("ClientID")},
            }
        )
    return items


def plan_new_aquira_clients(hubspot_companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for company in hubspot_companies:
        if company.get("aquira_id"):
            continue
        properties = company.get("properties") or {}
        name = str(properties.get("name") or company.get("name") or "New company")
        items.append(
            {
                "entityType": "client",
                "aquiraId": None,
                "hubspotId": company.get("hubspotId"),
                "action": "create",
                "name": name,
                "diffs": [{"field": "Name", "from": None, "to": name}],
                "properties": {
                    "Name": name,
                    "Phone": str(properties.get("phone") or ""),
                    "Website": str(properties.get("domain") or properties.get("website") or ""),
                    "PhysicalAddress": str(properties.get("address") or ""),
                },
                "writeback": True,
            }
        )
    return items
