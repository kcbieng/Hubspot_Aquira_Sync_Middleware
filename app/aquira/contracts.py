from __future__ import annotations

from typing import Any


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "Value" in value:
        return value.get("Value")
    return value


def _field_value(value: Any, default: Any = None) -> Any:
    unwrapped = _unwrap(value)
    if unwrapped is None:
        return default
    return unwrapped


def normalize_spotlines(raw_load: dict[str, Any], raw_detail: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = raw_load.get("Entity", {})
    raw_lines = raw_detail.get("lines", []) if isinstance(raw_detail, dict) else []
    lines: list[dict[str, Any]] = []

    for line in raw_lines:
        station_ref = _field_value(line.get("Station"), {})
        if isinstance(station_ref, dict):
            station_name = _field_value(station_ref.get("Name"), "ALL")
            station_id = _field_value(station_ref.get("ID"), _field_value(line.get("StationID"), 0))
        else:
            station_name = "ALL"
            station_id = _field_value(line.get("StationID"), 0)

        lines.append(
            {
                "station_id": station_id,
                "station": station_name,
                "start": _field_value(line.get("StartDate")) or _field_value(line.get("start")),
                "end": _field_value(line.get("EndDate")) or _field_value(line.get("end")),
                "amount": _field_value(line.get("Amount")) or _field_value(line.get("amount"), 0),
                "spots_by_month": _field_value(line.get("SpotsByMonth"), {}) or {},
                "seconds_by_month": _field_value(line.get("SecondsByMonth"), {}) or {},
            }
        )

    normalized = {
        "contract_id": contract.get("ID"),
        "contract_cd": _field_value(contract.get("ContractCD")),
        "kind": "booked" if contract.get("IsContract") else "proposal",
        "fallback_start": _field_value(contract.get("StartDate")),
        "fallback_end": _field_value(contract.get("EndDate")),
        "fallback_amount": _field_value(contract.get("TotalValue")),
        "lines": lines,
    }
    return normalized
