from __future__ import annotations

from typing import Any


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "Value" in value:
        return value.get("Value")
    return value


class SyncPlanner:
    """Builds safe plan objects without mutating remote systems in what-if mode."""

    def _field_diff(self, current: dict[str, Any], proposed: dict[str, Any]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        preferred_order = ["Name", "Email", "Phone", "Website", "Address", "PhysicalAddress", "ShortName"]

        keys = list(set(current) | set(proposed))
        keys.sort(key=lambda key: (preferred_order.index(key) if key in preferred_order else len(preferred_order), key.lower()))

        for key in keys:
            current_value = _unwrap(current.get(key))
            proposed_value = _unwrap(proposed.get(key))
            if current_value != proposed_value:
                changes.append({"field": key, "from": current_value, "to": proposed_value})
        return changes

    def _party_type(self, client: dict[str, Any] | None) -> str:
        if not client:
            return "unknown"
        is_account = bool(_unwrap(client.get("IsAccount")) is True)
        is_advertiser = bool(_unwrap(client.get("IsAdvertiser")) is True)
        if is_account and is_advertiser:
            return "both"
        if is_account:
            return "account"
        if is_advertiser:
            return "advertiser"
        return "unknown"

    def plan_client_update(self, aquira_id: int, current: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
        return {
            "entity": "client",
            "aquira_id": aquira_id,
            "action": "update",
            "keys": {"aquira_id": aquira_id},
            "field_diff": self._field_diff(current, proposed),
        }

    def plan_client_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        created = {"Name": payload.get("Name")}
        for key, value in payload.items():
            if key not in {"ID", "Name"}:
                created[key] = value
        return {
            "entity": "client",
            "aquira_id": payload.get("ID"),
            "action": "create",
            "keys": {"aquira_id": payload.get("ID")},
            "field_diff": self._field_diff({}, created),
        }

    def plan_company_upsert(self, account_client: dict[str, Any] | None, advertiser_client: dict[str, Any] | None = None) -> dict[str, Any]:
        base_client = account_client or advertiser_client or {}
        aquira_id = _unwrap(base_client.get("ID"))
        party_type = self._party_type(account_client or advertiser_client)
        if account_client and advertiser_client:
            account_id = _unwrap(account_client.get("ID"))
            advertiser_id = _unwrap(advertiser_client.get("ID"))
            needs_parent = account_id != advertiser_id
        else:
            account_id = _unwrap((account_client or {}).get("ID"))
            advertiser_id = _unwrap((advertiser_client or {}).get("ID"))
            needs_parent = False

        return {
            "entity": "company",
            "action": "upsert",
            "aquira_id": aquira_id,
            "aquira_party_type": party_type,
            "keys": {"aquira_id": aquira_id},
            "field_diff": self._field_diff({}, {"Name": _unwrap(base_client.get("Name")) or "", "aquira_party_type": party_type}),
            "account_id": account_id,
            "advertiser_id": advertiser_id,
            "needs_parent": needs_parent,
        }
