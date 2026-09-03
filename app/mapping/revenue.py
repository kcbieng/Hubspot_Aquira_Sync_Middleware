from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any


def _to_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            return None


def _month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _month_slice(start: date, end: date) -> list[date]:
    months: list[date] = []
    cur = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    while cur <= final:
        months.append(cur)
        year = cur.year + (cur.month // 12)
        month = 1 if cur.month == 12 else cur.month + 1
        cur = date(year, month, 1)
    return months


def _first_of_next_month(d: date) -> date:
    year = d.year + (d.month // 12)
    month = 1 if d.month == 12 else d.month + 1
    return date(year, month, 1)


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def allocate_revenue(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    """Allocate a contract amount across touched months according to the locked revenue-period algorithm."""
    contract_id = normalized.get("contract_id")
    contract_cd = normalized.get("contract_cd") or ""
    kind = normalized.get("kind") or "booked"
    lines = normalized.get("lines") or []

    if not lines:
        start = _to_date(normalized.get("fallback_start"))
        end = _to_date(normalized.get("fallback_end"))
        amount = Decimal(str(normalized.get("fallback_amount") or 0))
        if start is None or end is None:
            return []
        totals: dict[tuple[int, str, int], Decimal] = defaultdict(Decimal)
        months = _month_slice(start, end)
        per_month = _round_money(amount / Decimal(len(months)))
        remainder = amount - (per_month * Decimal(len(months)))
        for idx, month_start in enumerate(months):
            month_amount = per_month + (Decimal("0.01") if idx == len(months) - 1 and remainder > 0 else Decimal("0"))
            totals[(contract_id, _month_key(month_start), 0)] += month_amount
        return [
            {
                "aquira_id": f"{contract_id}:{month}:{station_id}",
                "period": month_start.strftime("%Y-%m-01"),
                "amount": float(amount_value),
                "station": "ALL",
                "station_id": station_id,
                "kind": kind,
                "contract_cd": contract_cd,
                "contract_id": contract_id,
            }
            for (c_id, month, station_id), amount_value in sorted(totals.items())
            for month_start in [date.fromisoformat(f"{month}-01")]
        ]

    totals: dict[tuple[int, str, int], Decimal] = defaultdict(Decimal)
    for line in lines:
        start = _to_date(line.get("start"))
        end = _to_date(line.get("end"))
        amount = Decimal(str(line.get("amount") or 0))
        station_id = int(line.get("station_id") or 0)
        station = line.get("station") or "ALL"
        if start is None or end is None:
            continue

        spaces = _month_slice(start, end)
        weights: dict[str, Decimal] = defaultdict(Decimal)
        spots_by_month = line.get("spots_by_month") or {}
        seconds_by_month = line.get("seconds_by_month") or {}

        if spots_by_month or seconds_by_month:
            for month_start in spaces:
                key = _month_key(month_start)
                if key in spots_by_month:
                    weights[key] = Decimal(str(spots_by_month[key]))
                elif key in seconds_by_month:
                    weights[key] = Decimal(str(seconds_by_month[key]))
            if not weights:
                weights = {key: Decimal("1") for key in {_month_key(d) for d in spaces}}
        else:
            for month_start in spaces:
                weights[_month_key(month_start)] = Decimal("1")

        weight_total = sum(weights.values())
        if weight_total == 0:
            weight_total = Decimal(len(weights))
            weights = {k: Decimal("1") for k in weights}

        running = Decimal("0")
        ordered = sorted(weights.items(), key=lambda item: item[0])
        for idx, (month_key, weight) in enumerate(ordered):
            share = amount * (Decimal(weight) / Decimal(weight_total))
            if idx == len(ordered) - 1:
                share = amount - running
            totals[(contract_id, month_key, station_id)] += share
            running += share

    periods: list[dict[str, Any]] = []
    for (c_id, month, station_id), amount_value in sorted(totals.items()):
        periods.append(
            {
                "aquira_id": f"{c_id}:{month}:{station_id}",
                "period": f"{month}-01",
                "amount": float(_round_money(amount_value)),
                "station": "ALL" if station_id == 0 else "KCBI",
                "station_id": station_id,
                "kind": kind,
                "contract_cd": contract_cd,
                "contract_id": c_id,
            }
        )
    return periods
