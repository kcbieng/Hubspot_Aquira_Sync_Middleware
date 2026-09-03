from __future__ import annotations

from typing import Any


class SyncInProgress(RuntimeError):
    pass


def diff_props(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[dict[str, Any]]:
    old_map = old or {}
    new_map = new or {}
    changes: list[dict[str, Any]] = []
    for key in sorted(set(old_map) | set(new_map)):
        if old_map.get(key) != new_map.get(key):
            changes.append({"field": key, "from": old_map.get(key), "to": new_map.get(key)})
    return changes


class WhatIfPlanner:
    def plan_create(self, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"entity": entity, "action": "create", "keys": payload.get("keys") or {"id": payload.get("id")}, "field_diff": payload.get("field_diff") or payload}

    def plan_update(self, entity: str, key: str, old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
        old_map = old or {}
        new_map = new or {}
        key_value = old_map.get(key, new_map.get(key))
        return {"entity": entity, "action": "update", "keys": {key: key_value}, "field_diff": diff_props(old, new)}

    def plan_skip(self, entity: str, reason: str) -> dict[str, Any]:
        return {"entity": entity, "action": "skip", "keys": {}, "field_diff": [{"field": "reason", "from": None, "to": reason}]}

    def plan_delete_stale(self, entity: str, stale_keys: list[str]) -> dict[str, Any]:
        return {"entity": entity, "action": "delete-stale", "keys": stale_keys, "field_diff": []}
