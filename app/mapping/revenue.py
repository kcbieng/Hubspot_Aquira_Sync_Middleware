from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

MISMATCH_TOLERANCE = Decimal("0.05")


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


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def contract_revenue_input(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_id": contract.get("ID"),
        "contract_cd": contract.get("ContractCD"),
        "kind": "booked" if contract.get("IsContract") else "proposal",
        "fallback_start": contract.get("StartDate"),
        "fallback_end": contract.get("EndDate"),
        "fallback_amount": contract.get("TotalValue"),
        "lines": contract.get("lines") or [],
    }


def _empty_bucket(station: str) -> dict[str, Any]:
    return {"amount": Decimal("0"), "spot": Decimal("0"), "charge": Decimal("0"), "station": station or "ALL"}


def _period_row(
    contract_id: Any,
    month: str,
    station_id: int,
    bucket: dict[str, Any],
    kind: str,
    contract_cd: str,
) -> dict[str, Any]:
    amount = _round_money(_money(bucket.get("amount")))
    spot = _round_money(_money(bucket.get("spot")))
    charge = _round_money(_money(bucket.get("charge")))
    source = "mixed"
    if charge and not spot:
        source = "charge"
    elif spot and not charge:
        source = "spot"
    elif not spot and not charge:
        source = "spot"
    return {
        "aquira_id": f"{contract_id}:{month}:{station_id}",
        "period": f"{month}-01",
        "amount": float(amount),
        "spot_amount": float(spot),
        "charge_amount": float(charge),
        "source": source,
        "station": bucket.get("station") or ("ALL" if station_id == 0 else "KCBI"),
        "station_id": station_id,
        "kind": kind,
        "contract_cd": contract_cd,
        "contract_id": contract_id,
        "deal_aquira_id": str(contract_id or ""),
    }


def _scale_down_to_total(periods: list[dict[str, Any]], target: Any) -> list[dict[str, Any]]:
    """Booked-if-all-play often exceeds contract Total Amount after missed spots. Scale down only."""
    target_d = _round_money(_money(target))
    if not periods or target_d <= 0:
        return periods
    allocated = sum((_money(period.get("amount")) for period in periods), Decimal("0"))
    if allocated <= target_d + MISMATCH_TOLERANCE:
        return periods
    factor = target_d / allocated
    running = Decimal("0")
    scaled: list[dict[str, Any]] = []
    for idx, period in enumerate(periods):
        old = _money(period.get("amount"))
        if idx == len(periods) - 1:
            new_amt = target_d - running
        else:
            new_amt = _round_money(old * factor)
            running += new_amt
        ratio = (new_amt / old) if old else Decimal("0")
        row = dict(period)
        row["amount"] = float(new_amt)
        row["spot_amount"] = float(_round_money(_money(period.get("spot_amount")) * ratio))
        row["charge_amount"] = float(_round_money(_money(period.get("charge_amount")) * ratio))
        scaled.append(row)
    return scaled


def allocate_revenue(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    """Allocate spot lines and charge lines across the months each line actually covers."""
    contract_id = normalized.get("contract_id")
    contract_cd = normalized.get("contract_cd") or ""
    kind = normalized.get("kind") or "booked"
    lines = normalized.get("lines") or []

    if not lines:
        start = _to_date(normalized.get("fallback_start"))
        end = _to_date(normalized.get("fallback_end"))
        amount = _money(normalized.get("fallback_amount"))
        if start is None or end is None or amount == 0:
            return []
        months = _month_slice(start, end)
        per_month = _round_money(amount / Decimal(len(months)))
        running = Decimal("0")
        periods: list[dict[str, Any]] = []
        for idx, month_start in enumerate(months):
            share = amount - running if idx == len(months) - 1 else per_month
            running += share
            month = _month_key(month_start)
            periods.append(
                _period_row(
                    contract_id,
                    month,
                    0,
                    {"amount": share, "spot": share, "charge": Decimal("0"), "station": "ALL"},
                    kind,
                    contract_cd,
                )
            )
        return periods

    totals: dict[tuple[Any, str, int], dict[str, Any]] = {}
    for line in lines:
        start = _to_date(line.get("start"))
        end = _to_date(line.get("end")) or start
        amount = _money(line.get("amount"))
        station_id = int(line.get("station_id") or 0)
        station = str(line.get("station") or "ALL")
        line_kind = str(line.get("line_kind") or "spot")
        if start is None or amount == 0:
            continue

        spaces = _month_slice(start, end)
        weights: dict[str, Decimal] = defaultdict(Decimal)
        spots_by_month = line.get("spots_by_month") or {}
        seconds_by_month = line.get("seconds_by_month") or {}

        if spots_by_month or seconds_by_month:
            for month_start in spaces:
                key = _month_key(month_start)
                if key in spots_by_month:
                    weights[key] = _money(spots_by_month[key])
                elif key in seconds_by_month:
                    weights[key] = _money(seconds_by_month[key])
            if not weights:
                weights = {key: Decimal("1") for key in {_month_key(d) for d in spaces}}
        else:
            for month_start in spaces:
                weights[_month_key(month_start)] = Decimal("1")

        weight_total = sum(weights.values())
        if weight_total == 0:
            weight_total = Decimal(len(weights) or 1)
            weights = {k: Decimal("1") for k in weights} or {_month_key(start): Decimal("1")}

        running = Decimal("0")
        ordered = sorted(weights.items(), key=lambda item: item[0])
        for idx, (month_key, weight) in enumerate(ordered):
            share = amount * (Decimal(weight) / Decimal(weight_total))
            if idx == len(ordered) - 1:
                share = amount - running
            bucket = totals.setdefault((contract_id, month_key, station_id), _empty_bucket(station))
            bucket["amount"] += share
            bucket[line_kind if line_kind in {"spot", "charge"} else "spot"] += share
            if station and station != "ALL":
                bucket["station"] = station
            running += share

    return _scale_down_to_total(
        [
            _period_row(c_id, month, station_id, bucket, kind, contract_cd)
            for (c_id, month, station_id), bucket in sorted(totals.items())
        ],
        normalized.get("fallback_amount"),
    )


def summarize_allocation(contract: dict[str, Any], periods: list[dict[str, Any]]) -> dict[str, Any]:
    aquira = _round_money(_money(contract.get("TotalValue")))
    line_total = Decimal("0")
    spot_total = Decimal("0")
    charge_total = Decimal("0")
    for line in contract.get("lines") or []:
        amount = _money(line.get("amount"))
        line_total += amount
        if str(line.get("line_kind") or "spot") == "charge":
            charge_total += amount
        else:
            spot_total += amount
    allocated = sum((_money(period.get("amount")) for period in periods), Decimal("0"))
    if not (contract.get("lines") or []):
        line_total = allocated
        spot_total = allocated
        charge_total = Decimal("0")
    delta = aquira - _round_money(allocated)
    mismatch = abs(delta) > MISMATCH_TOLERANCE
    warning = ""
    if mismatch:
        warning = (
            f"Aquira contract total {float(aquira)} != allocated "
            f"{float(_round_money(allocated))} (spot {float(_round_money(spot_total))} + "
            f"charge {float(_round_money(charge_total))}, delta {float(delta)})"
        )
    return {
        "aquira_total": float(aquira),
        "line_total": float(_round_money(line_total)),
        "spot_total": float(_round_money(spot_total)),
        "charge_total": float(_round_money(charge_total)),
        "allocated_total": float(_round_money(allocated)),
        "delta": float(delta),
        "mismatch": mismatch,
        "warning": warning,
    }
