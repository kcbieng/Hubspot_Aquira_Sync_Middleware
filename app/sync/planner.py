from __future__ import annotations

from typing import Any

from app.aquira.fieldvalues import unwrap
from app.hashutil import content_hash
from app.mapping.parties import party_type_for_client
from app.mapping.revenue import allocate_revenue, contract_revenue_input, summarize_allocation

IDENTITY_COMPANY_FIELDS = ("name", "phone", "domain", "address", "city", "state")
IDENTITY_CONTACT_FIELDS = ("firstname", "lastname", "email", "phone")

# str() residue that Aquira produces when a FieldValue cannot be unwrapped.
BLANK_STRINGS = {"", "{}", "[]", "()", "none", "null", "nan", "nat"}

# Money fields where a source value of 0 means "the read came back thin", not
# "this is genuinely zero". Only enforced against a non-zero existing value.
ZERO_IS_SUSPECT_FIELDS = {"amount"}


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (dict, list, tuple, set)):
        return True
    if isinstance(value, str):
        return value.strip().lower() in BLANK_STRINGS
    return False


def _suppress_blank_overwrite(
    entity_type: str,
    proposed: dict[str, Any],
    existing: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Drop proposed values that would blank out data HubSpot already holds.

    Aquira reads fail soft everywhere in this client — a 500 from
    GetContractDetailAnalysis becomes an empty line set, and a thin search row
    becomes a contract with no TotalValue. Without this guard the next PATCH
    writes 0 or "" over real money and real identity, and because
    plan_companies/plan_contacts let HubSpot win once a value is non-empty, a
    blank written at create time is sticky forever.
    """
    current = (existing or {}).get("properties") or {}
    kept: dict[str, Any] = {}
    suppressed: list[str] = []
    for key, value in proposed.items():
        previous = current.get(key)
        if previous is not None and not _is_blank(previous):
            if _is_blank(value):
                suppressed.append(key)
                continue
            if key in ZERO_IS_SUSPECT_FIELDS:
                try:
                    if float(value) == 0 and float(previous) != 0:
                        suppressed.append(key)
                        continue
                except (TypeError, ValueError):
                    pass
        kept[key] = value
    return kept, suppressed


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


# ---------------------------------------------------------------------------
# Field ownership: reps work the pipeline board and identity records in
# HubSpot; the sync may only re-push an owned field when the value Aquira
# DERIVED last sync differs from what it derives now (a source transition),
# never merely because a human moved it. Baselines come from EntitySnapshot:
# the last-sync state of both sides. Without a snapshot for an existing
# record, the human's current state is adopted silently — the first run
# after this deploy never yanks anyone's board around.
# ---------------------------------------------------------------------------
HUMAN_MANAGED_DEAL_FIELDS = {"dealname", "pipeline", "dealstage"}

COMPANY_IDENTITY_PAIRS = (("Name", "name"), ("Phone", "phone"), ("Website", "domain"), ("PhysicalAddress", "address"))
CONTACT_IDENTITY_PAIRS = (("FirstName", "firstname"), ("LastName", "lastname"), ("Email", "email"), ("Phone", "phone"))


def apply_deal_field_ownership(
    derived: dict[str, Any],
    existing: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Returns (props_to_write, preserved_fields)."""
    if existing is None:
        return dict(derived), []
    last_aquira = (snapshot or {}).get("aquira") or {}
    kept = dict(derived)
    preserved: list[str] = []
    for field in sorted(HUMAN_MANAGED_DEAL_FIELDS & set(derived)):
        if snapshot is None or _same(last_aquira.get(field), derived.get(field)):
            kept.pop(field, None)
            preserved.append(field)
    return kept, preserved


def _three_way_fields(
    hs_props: dict[str, Any],
    aq_row: dict[str, Any],
    snapshot: dict[str, Any] | None,
    pairs: tuple[tuple[str, str], ...],
) -> tuple[dict[str, Any], list[str]]:
    """Per-field 3-way merge of a HubSpot-side value vs an Aquira-side value against
    the last-sync baseline. Returns (fields_to_write_back, conflicts)."""
    proposed: dict[str, Any] = {}
    conflicts: list[str] = []
    if snapshot is None:
        # No baseline yet: keep the historical HubSpot-wins seeding so behavior
        # only changes where we actually know what each side used to hold.
        for aq_field, hs_field in pairs:
            hs_now = str(hs_props.get(hs_field) or "").strip()
            aq_now = str(aq_row.get(aq_field) or "").strip()
            if hs_now and hs_now != aq_now:
                proposed[aq_field] = hs_now
        return proposed, conflicts
    aq_then = snapshot.get("aquira") or {}
    hs_then = snapshot.get("hubspot") or {}
    for aq_field, hs_field in pairs:
        aq_now = str(aq_row.get(aq_field) or "").strip()
        hs_now = str(hs_props.get(hs_field) or "").strip()
        aq_moved = not _same(aq_then.get(aq_field), aq_now)
        hs_moved = not _same(hs_then.get(hs_field), hs_now)
        if hs_moved and aq_moved and not _same(aq_now, hs_now):
            conflicts.append(aq_field)
        elif hs_moved and not aq_moved and hs_now:
            proposed[aq_field] = hs_now
    return proposed, conflicts


def company_properties(client: dict[str, Any]) -> dict[str, Any]:
    website = str(client.get("Website") or "")
    domain = website.replace("https://", "").replace("http://", "")
    props = {
        "name": client.get("Name") or "",
        "domain": domain,
        "phone": client.get("Phone") or "",
        "address": client.get("PhysicalAddress") or "",
        "city": client.get("City") or "",
        "state": client.get("State") or "",
        "aquira_id": str(client.get("ID")),
        "aquira_client_cd": str(client.get("ClientCD") or ""),
        "aquira_party_type": party_type_for_client(client),
        "aquira_version": client.get("Version"),
    }
    if client.get("HubSpotTeam"):
        props["aquira_hubspot_team"] = str(client.get("HubSpotTeam"))
    if client.get("hubspot_owner_id"):
        props["hubspot_owner_id"] = str(client.get("hubspot_owner_id"))
    return props


def contact_properties(contact: dict[str, Any]) -> dict[str, Any]:
    props = {
        "firstname": contact.get("FirstName") or "",
        "lastname": contact.get("LastName") or "",
        "email": str(contact.get("Email") or "").lower(),
        "phone": contact.get("Phone") or "",
        "aquira_id": str(contact.get("ID")),
        "aquira_entity_type": "contact",
        "aquira_client_id": str(contact.get("ClientID") or ""),
    }
    if contact.get("HubSpotTeam"):
        props["aquira_hubspot_team"] = str(contact.get("HubSpotTeam"))
    if contact.get("hubspot_owner_id"):
        props["hubspot_owner_id"] = str(contact.get("hubspot_owner_id"))
    return props


def deal_properties(contract: dict[str, Any], advertiser_name: str | None = None, stage_map: dict[str, str] | None = None) -> dict[str, Any]:
    if contract.get("allocated_total") is None:
        attach_revenue_summary(contract)
    description = str(contract.get("Description") or "").strip()
    advertiser = advertiser_name or contract.get("Name") or "Contract"
    label = description or advertiser
    cancelled = bool(contract.get("Cancelled"))
    is_contract = bool(contract.get("IsContract"))
    is_proposal = bool(contract.get("IsProposal")) and not is_contract
    # IsActive False comes from the UI-verified IsActiveFlag on Search rows.
    # An inactive proposal is a dead letter: it is closed-lost on the board,
    # and because the stage is derived from the source, reactivating in Aquira
    # flips IsActiveFlag back and the transition-writes rule re-opens it.
    dead_proposal = contract.get("IsActive") is False and is_proposal
    stage = "closedlost" if cancelled or dead_proposal else "closedwon" if is_contract else "proposal"
    pipeline = "default"
    if stage_map:
        # Custom pipelines give stages opaque GUID ids; the semantic
        # closedwon/closedlost/proposal tokens only exist in the default one.
        pipeline = str(stage_map.get("pipeline") or "default")
        stage = str(stage_map.get(stage) or stage)
    props = {
        "dealname": f"{contract.get('ContractCD')} — {label}",
        "amount": contract.get("TotalValue") or 0,
        "closedate": contract.get("EndDate") or "",
        "pipeline": pipeline,
        "dealstage": stage,
        "aquira_id": str(contract.get("ID")),
        "aquira_contract_cd": contract.get("ContractCD") or "",
        "aquira_status": contract.get("Status") or ("Booked" if is_contract else "Proposal"),
        "aquira_is_proposal": bool(contract.get("IsProposal")),
        "aquira_is_contract": is_contract,
        "aquira_is_active": True if contract.get("IsActive") is None else bool(contract.get("IsActive")),
        "aquira_sign_date": contract.get("SignDate") or "",
        "aquira_start_date": contract.get("StartDate") or "",
        "aquira_end_date": contract.get("EndDate") or "",
        "aquira_stations": contract.get("Stations") or "KCBI",
        "aquira_account_id": str(contract.get("AccountID") or ""),
        "aquira_advertiser_id": str(contract.get("AdvertiserID") or ""),
        "aquira_sales_rep": str(contract.get("SalesRepID") or ""),
        "aquira_allocated_amount": contract.get("allocated_total") if contract.get("allocated_total") is not None else None,
        "aquira_line_total": contract.get("line_total") if contract.get("line_total") is not None else None,
        "aquira_spot_total": contract.get("spot_total") if contract.get("spot_total") is not None else None,
        "aquira_charge_total": contract.get("charge_total") if contract.get("charge_total") is not None else None,
        "aquira_booked_amount": contract.get("booked_total") if contract.get("booked_total") is not None else None,
        "aquira_amount_delta": contract.get("amount_delta") if contract.get("amount_delta") is not None else None,
        "aquira_amount_mismatch": bool(contract.get("amount_mismatch")),
    }
    props = {key: value for key, value in props.items() if value is not None}
    if contract.get("HubSpotTeam"):
        props["aquira_hubspot_team"] = str(contract.get("HubSpotTeam"))
    if contract.get("hubspot_owner_id"):
        props["hubspot_owner_id"] = str(contract.get("hubspot_owner_id"))
    return props


def plan_upsert(
    entity_type: str,
    aquira_id: str,
    name: str,
    proposed: dict[str, Any],
    existing: dict[str, Any] | None,
    associations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    proposed, suppressed = _suppress_blank_overwrite(entity_type, proposed, existing)
    digest = content_hash(proposed)
    existing_hash = (existing or {}).get("hash")
    diffs = [
        row
        for row in field_diff((existing or {}).get("properties") or {}, proposed)
        if row["field"] != "aquira_version"
    ]
    unchanged = bool(existing) and (existing_hash == digest or not diffs)
    hubspot_id = str((existing or {}).get("hubspotId") or "").strip()
    if unchanged and hubspot_id:
        return {
            "entityType": entity_type,
            "aquiraId": aquira_id,
            "hubspotId": existing.get("hubspotId") if existing else None,
            "action": "skip",
            "name": name,
            "diffs": [],
            "properties": proposed,
            "associations": associations,
            "suppressed": suppressed,
            "warning": (
                f"Withheld blank/zero overwrite for {', '.join(sorted(suppressed))} "
                f"on {entity_type} {aquira_id} — the Aquira read looks thin, not empty"
            ) if suppressed else None,
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
        "suppressed": suppressed,
        "warning": (
            f"Withheld blank/zero overwrite for {', '.join(sorted(suppressed))} "
            f"on {entity_type} {aquira_id} — the Aquira read looks thin, not empty"
        ) if suppressed else None,
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
        parent = client.get("AccountID")
        if parent and str(parent) != str(client.get("ID")):
            account_id = str(parent)
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


def attach_revenue_summary(contract: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    periods = allocate_revenue(contract_revenue_input(contract))
    summary = summarize_allocation(contract, periods)
    booked = 0.0
    for line in contract.get("lines") or []:
        booked += float(line.get("booked_amount") or line.get("amount") or 0)
    contract["allocated_total"] = summary["allocated_total"]
    contract["line_total"] = summary["line_total"]
    contract["spot_total"] = summary["spot_total"]
    contract["charge_total"] = summary["charge_total"]
    contract["booked_total"] = round(booked, 2) if booked else summary["line_total"]
    contract["amount_delta"] = summary["delta"]
    contract["amount_mismatch"] = summary["mismatch"]
    contract["amount_warning"] = summary["warning"]
    return periods, summary


def plan_deals(
    contracts: list[dict[str, Any]],
    existing_by_aquira: dict[str, dict[str, Any]],
    owner_by_aquira_user: dict[str, str],
    client_name_by_id: dict[str, str] | None = None,
    snapshots: dict[str, dict[str, Any]] | None = None,
    stage_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    names = client_name_by_id or {}
    snap_map = snapshots or {}
    for contract in contracts:
        if contract.get("IsActive") is False:
            # Deactivated in Aquira = deleted in the UI. plan_inactive_deal_archives
            # owns these records; upserting against an archived deal only errors.
            continue
        attach_revenue_summary(contract)
        advertiser_name = names.get(str(contract.get("AdvertiserID")))
        props = deal_properties(contract, advertiser_name, stage_map)
        owner_id = None
        if contract.get("SalesRepID"):
            owner_id = owner_by_aquira_user.get(str(contract.get("SalesRepID")))
        owner_id = owner_id or contract.get("hubspot_owner_id")
        if owner_id:
            props["hubspot_owner_id"] = str(owner_id)
        company_ids = list({str(contract.get("AccountID") or ""), str(contract.get("AdvertiserID") or "")} - {""})
        aid = str(contract.get("ID"))
        existing = existing_by_aquira.get(aid)
        to_write, preserved = apply_deal_field_ownership(props, existing, snap_map.get(aid))
        item = plan_upsert(
            "deal",
            aid,
            str(props.get("dealname") or ""),
            to_write,
            existing,
            {"companyIds": company_ids, "ownerId": owner_id},
        )
        item["preserved"] = preserved
        # What the SOURCE derives right now — persisted as the Aquira-side
        # baseline so the next run can tell its own transitions from rep moves.
        item["aquiraDerived"] = {f: props.get(f) for f in HUMAN_MANAGED_DEAL_FIELDS}
        if (existing or {}).get("archived") and item.get("action") in {"update", "skip"}:
            # A reactivated contract whose deal we previously archived: restore
            # it (apply unarchives, then falls through to update) instead of
            # creating a second deal for the same record. Even a property-for-
            # property "skip" must unarchive — archived is not a state to skip.
            item["action"] = "unarchive"
        if contract.get("amount_warning"):
            item["warning"] = contract["amount_warning"]
        items.append(item)
    return items


def plan_missing_deals(
    deals_by_aquira: dict[str, dict[str, Any]],
    catalog_contract_ids: set[str],
    *,
    allow_archive: bool,
) -> list[dict[str, Any]]:
    """HubSpot deals whose Aquira contract vanished from a COMPLETE enumeration.

    Aquira deactivates rather than deletes, so a disappearance is either an
    actual deletion or a pull that missed rows — this only ever runs when the
    caller certified the pull (allow_archive), and it archives (recoverable),
    never hard-deletes.
    """
    if not allow_archive:
        return []
    items: list[dict[str, Any]] = []
    for aid, entry in deals_by_aquira.items():
        if str(aid) in catalog_contract_ids:
            continue
        hubspot_id = str(entry.get("hubspotId") or "")
        if not hubspot_id:
            continue
        properties = entry.get("properties") or {}
        items.append(
            {
                "entityType": "deal",
                "aquiraId": str(aid),
                "hubspotId": hubspot_id,
                "action": "archive",
                "name": str(properties.get("dealname") or f"Aquira deal {aid}"),
                "diffs": [{"field": "presence", "from": "in HubSpot", "to": "absent from certified Aquira catalog"}],
                "properties": {},
            }
        )
    return items


def plan_inactive_deal_archives(
    contracts: list[dict[str, Any]],
    deals_by_aquira: dict[str, dict[str, Any]],
    *,
    allow_archive: bool,
) -> list[dict[str, Any]]:
    """Inactive is Aquira's delete button: no hard delete exists, staff
    deactivate and the row vanishes behind UI filters. Mirror that in HubSpot
    by archiving the deal — recoverable, so a reactivation unarchives the
    same record instead of creating a twin. Revenue-period fate is decided in
    plan_revenue: booked contracts keep their periods for historical
    modelling; proposals get theirs purged to save records."""
    if not allow_archive:
        return []
    items: list[dict[str, Any]] = []
    for contract in contracts:
        if contract.get("IsActive") is not False:
            continue
        aid = str(contract.get("ID") or "")
        entry = deals_by_aquira.get(aid) or {}
        hubspot_id = str(entry.get("hubspotId") or "")
        if not hubspot_id or entry.get("archived"):
            continue
        properties = entry.get("properties") or {}
        items.append(
            {
                "entityType": "deal",
                "aquiraId": aid,
                "hubspotId": hubspot_id,
                "action": "archive",
                "name": str(properties.get("dealname") or contract.get("ContractCD") or f"Aquira deal {aid}"),
                "diffs": [{"field": "IsActive", "from": True, "to": False}],
                "properties": {},
            }
        )
    return items


def _revenue_contract_id(aquira_id: str, properties: dict[str, Any] | None = None) -> str:
    props = properties or {}
    deal_id = str(props.get("deal_aquira_id") or "").strip()
    if deal_id:
        return deal_id
    text = str(aquira_id or "")
    if ":" in text:
        return text.split(":", 1)[0]
    return text


def plan_revenue(
    contracts: list[dict[str, Any]],
    existing_by_aquira: dict[str, dict[str, Any]],
    *,
    prune_stale: bool = True,
    only_contract_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    produced: set[str] = set()
    for contract in contracts:
        if contract.get("IsActive") is False:
            # Produce nothing for deactivated records. Whether their existing
            # periods survive is the caller's scope decision: booked contracts
            # are dropped from only_contract_ids (hold for historical
            # modelling), inactive proposals stay in scope, so every period
            # they still have is stale and gets purged.
            continue
        periods, _summary = attach_revenue_summary(contract)
        company_ids: list[str] = []
        for raw in (contract.get("AccountID"), contract.get("AdvertiserID")):
            text = str(raw or "").strip()
            if text and text not in company_ids:
                company_ids.append(text)
        for period in periods:
            produced.add(period["aquira_id"])
            props = {
                "aquira_id": period["aquira_id"],
                "period": period["period"],
                "amount": period["amount"],
                "spot_amount": period.get("spot_amount") or 0,
                "charge_amount": period.get("charge_amount") or 0,
                "source": period.get("source") or "spot",
                "station": period["station"],
                "station_id": period["station_id"],
                "kind": period["kind"],
                "contract_cd": period["contract_cd"],
                "deal_aquira_id": str(contract.get("ID") or ""),
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
                        "companyIds": company_ids,
                    },
                )
            )

    if prune_stale:
        for aquira_id, existing in existing_by_aquira.items():
            if aquira_id in produced:
                continue
            owner = _revenue_contract_id(aquira_id, existing.get("properties") or {})
            if only_contract_ids is not None and owner not in only_contract_ids:
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


def plan_identity_writebacks(
    hubspot_companies: list[dict[str, Any]],
    aquira_by_id: dict[str, dict[str, Any]],
    snapshots: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    snap_map = snapshots or {}
    for company in hubspot_companies:
        aid = str(company.get("aquira_id") or "")
        client = aquira_by_id.get(aid)
        if not client:
            continue
        properties = company.get("properties") or {}
        proposed, conflicts = _three_way_fields(properties, client, snap_map.get(aid), COMPANY_IDENTITY_PAIRS)
        current = {
            "Name": client.get("Name") or "",
            "Phone": client.get("Phone") or "",
            "Website": client.get("Website") or "",
            "PhysicalAddress": client.get("PhysicalAddress") or "",
        }
        diffs = field_diff(current, proposed)
        if not diffs:
            if conflicts:
                items.append(
                    {
                        "entityType": "client",
                        "aquiraId": aid,
                        "hubspotId": company.get("hubspotId"),
                        "action": "skip",
                        "name": client.get("Name") or "",
                        "diffs": [],
                        "properties": {},
                        "writeback": True,
                        "conflicts": conflicts,
                        "warning": (
                            f"Identity conflict on company {aid}: {', '.join(conflicts)} changed on BOTH "
                            "sides since the last sync — neither was written; reconcile one side manually"
                        ),
                    }
                )
            continue
        item = {
            "entityType": "client",
            "aquiraId": aid,
            "hubspotId": company.get("hubspotId"),
            "action": "update",
            "name": client.get("Name") or "",
            "diffs": diffs,
            "properties": proposed,
            "writeback": True,
        }
        if conflicts:
            item["conflicts"] = conflicts
            item["warning"] = (
                f"Identity conflict on company {aid}: {', '.join(conflicts)} changed on BOTH sides "
                "since the last sync — those fields were left alone; reconcile manually"
            )
        items.append(item)
    return items


def plan_contact_writebacks(
    hubspot_contacts: list[dict[str, Any]],
    aquira_by_id: dict[str, dict[str, Any]],
    snapshots: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    snap_map = snapshots or {}
    for row in hubspot_contacts:
        aid = str(row.get("aquira_id") or "")
        contact = aquira_by_id.get(aid)
        if not contact:
            continue
        properties = row.get("properties") or {}
        proposed, conflicts = _three_way_fields(properties, contact, snap_map.get(aid), CONTACT_IDENTITY_PAIRS)
        current = {
            "FirstName": contact.get("FirstName") or "",
            "LastName": contact.get("LastName") or "",
            "Email": contact.get("Email") or "",
            "Phone": contact.get("Phone") or "",
        }
        diffs = field_diff(current, proposed)
        if not diffs:
            if conflicts:
                items.append(
                    {
                        "entityType": "contact",
                        "aquiraId": aid,
                        "hubspotId": row.get("hubspotId"),
                        "action": "skip",
                        "name": f"{contact.get('FirstName')} {contact.get('LastName')}".strip(),
                        "diffs": [],
                        "properties": {},
                        "writeback": True,
                        "conflicts": conflicts,
                        "warning": (
                            f"Identity conflict on contact {aid}: {', '.join(conflicts)} changed on BOTH "
                            "sides since the last sync — neither was written; reconcile one side manually"
                        ),
                    }
                )
            continue
        item = {
            "entityType": "contact",
            "aquiraId": aid,
            "hubspotId": row.get("hubspotId"),
            "action": "update",
            "name": f"{contact.get('FirstName')} {contact.get('LastName')}".strip(),
            "diffs": diffs,
            "properties": proposed,
            "writeback": True,
            "associations": {"clientId": contact.get("ClientID")},
        }
        if conflicts:
            item["conflicts"] = conflicts
            item["warning"] = (
                f"Identity conflict on contact {aid}: {', '.join(conflicts)} changed on BOTH sides "
                "since the last sync — those fields were left alone; reconcile manually"
            )
        items.append(item)
    return items


def plan_new_aquira_clients(
    hubspot_companies: list[dict[str, Any]],
    blocked_hubspot_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Approval-gated client creation: a HubSpot company becomes an Aquira
    client ONLY when a human sets “Create in Aquira as…” on it. The dropdown
    carries the Account-vs-Advertiser decision because Aquira cannot default it
    the way the old automatic path silently did (everything as Account).
    Companies with an unresolved create-failure dead letter are skipped until
    an operator clears them — no per-run retry storms into the master system."""
    blocked = blocked_hubspot_ids or set()
    items: list[dict[str, Any]] = []
    for company in hubspot_companies:
        if company.get("aquira_id"):
            continue
        properties = company.get("properties") or {}
        party = str(properties.get("aquira_create_as") or "").strip().lower()
        if party not in {"account", "advertiser", "both"}:
            continue
        hid = str(company.get("hubspotId") or company.get("id") or "")
        if hid and hid in blocked:
            continue
        name = str(properties.get("name") or company.get("name") or "New company")
        items.append(
            {
                "entityType": "client",
                "aquiraId": None,
                "hubspotId": company.get("hubspotId"),
                "action": "create",
                "name": name,
                "createAs": party,
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
