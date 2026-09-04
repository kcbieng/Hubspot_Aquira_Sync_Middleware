from __future__ import annotations

from typing import Any

from app.aquira.normalize import as_bool, unwrap


def party_type_for_client(client: dict[str, Any]) -> str:
    is_account = as_bool(client.get("IsAccount"))
    is_advertiser = as_bool(client.get("IsAdvertiser"))

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
        return {"account_id": unwrap(account_client.get("ID")), "advertiser_id": None, "needs_parent": False}

    account_id = unwrap(account_client.get("ID"))
    advertiser_id = unwrap(advertiser_client.get("ID"))
    parent = account_id != advertiser_id
    return {"account_id": account_id, "advertiser_id": advertiser_id, "needs_parent": parent}
