from __future__ import annotations

from typing import Any


class AquiraApiError(RuntimeError):
    """Raised when Aquira responds with a failed or invalid business envelope."""


def unwrap_field_value(value: Any) -> Any:
    """Unwrap Aquira FieldValue wrappers while preserving plain values."""
    if isinstance(value, dict) and "Value" in value:
        return value["Value"]
    return value


def validate_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the standard Aquira response envelope and raise on business failure."""
    success = bool(payload.get("Success", False))
    error_code = payload.get("Error")
    errors = payload.get("Errors")

    if success:
        return payload

    if error_code == -16 and errors:
        raise AquiraApiError(f"Aquira validation failed: {errors}")

    raise AquiraApiError(
        f"Aquira request failed (Error={error_code}, ErrorName={payload.get('ErrorName')}, Errors={errors})"
    )
