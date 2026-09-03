from __future__ import annotations

from typing import Any


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "Value" in value:
        return value.get("Value")
    return value


def party_type_for_client(client: dict[str, Any]) -> str:
    is_account = bool(_unwrap(client.get("IsAccount")) if isinstance(client.get("IsAccount"), (dict, bool)) else False)
    is_advertiser = bool(_unwrap(client.get("IsAdvertiser")) if isinstance(client.get("IsAdvertiser"), (dict, bool)) else False)

    if is_account and is_advertiser:
        return "both"
    if is_account:
        return "account"
    if is_advertiser:
        return "advertiser"
    return "unknown"


def link_account_advertiser(account_client: dict[str, Any] | None, advertiser_client: dict[str, Any] | None) -> dict[str, Any]:
    if not account_client:
        return {"account_id": None, "advertiser_id": None, "needs_parent": False}
    if not advertiser_client:
        return {"account_id": _unwrap(account_client.get("ID")), "advertiser_id": None, "needs_parent": False}

    account_id = _unwrap(account_client.get("ID"))
    advertiser_id = _unwrap(advertiser_client.get("ID"))
    parent = account_id != advertiser_id
    return {"account_id": account_id, "advertiser_id": advertiser_id, "needs_parent": parent}
