from __future__ import annotations

from typing import Any, Iterable

ACCESS_READ_ONLY = 1
ACCESS_READ_AND_WRITE = 2


def unwrap(value: Any) -> Any:
    """Unwrap Aquira FieldValue wrappers, returning plain values for downstream logic."""
    if isinstance(value, dict) and "Value" in value:
        return value.get("Value")
    return value


def sparse_put(entity: dict[str, Any], allowed_paths: Iterable[str]) -> dict[str, Any]:
    """Emit only the fields that are both writable and explicitly allowed."""
    allowed = set(allowed_paths)
    result: dict[str, Any] = {}
    for key, value in entity.items():
        if key not in allowed:
            continue
        if isinstance(value, dict):
            access = value.get("Access")
            if access == ACCESS_READ_AND_WRITE:
                result[key] = unwrap(value)
        else:
            result[key] = value
    return result


def field_diff(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[dict[str, Any]]:
    old_map = old or {}
    new_map = new or {}
    changes: list[dict[str, Any]] = []
    for key in sorted(set(old_map) | set(new_map)):
        old_val = unwrap(old_map.get(key))
        new_val = unwrap(new_map.get(key))
        if old_val != new_val:
            changes.append({"field": key, "from": old_val, "to": new_val})
    return changes
