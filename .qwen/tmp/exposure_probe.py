"""READ-ONLY exposure probe: does the unreadable spot-analysis envelope cost records?

Two defects were found while diagnosing the pruning notice:
  (A) normalize_spot_lines() cannot read {"Data": {"Items": [...]}} — it only accepts
      a bag when it is a list — so /Contract/GetSpotLineDetailAnalysis is a NO-OP read
      and load_spot_lines silently falls through to the /Contract/Load summary rows.
  (B) `certified` counted any swallowed optional read as pull damage.

(B) is fixed. (A) is the one that can now DELETE data: plan_revenue's prune_stale
removes every period of an ACTIVE contract that produced zero lines, and only
_detail_failed (a raised /Contract/Load) rescues it. So a contract whose revenue is
visible ONLY through the unreadable read would have its HubSpot periods pruned by the
first certified run.

This measures, per contract: what the app assembles today (contract["lines"]), what
the raw analysis payloads actually contain, and the size of the exposure. Uses the
app's own normalizers and its own load_contract path.

Reads only: SearchByID, /Contract/Load/<id>, the two analysis endpoints.
"""
import json
import sys
from collections import Counter
from typing import Any

import httpx

sys.path.insert(0, "/app")

from app.aquira.client import AquiraSessionClient  # noqa: E402
from app.aquira.normalize import (  # noqa: E402
    normalize_contract,
    normalize_revenue_months,
    normalize_spot_lines,
)
from app.settings import get_settings  # noqa: E402

s = get_settings()
base = (s.aquira_base_url or "").rstrip("/")
client = httpx.Client(base_url=base, timeout=60.0)
login = client.post("/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password}).json()
if not login.get("Success", True):
    print("login refused:", json.dumps(login)[:200])
    sys.exit(1)
print("login ok")


def call(path: str, body: dict | None = None) -> dict[str, Any]:
    try:
        r = client.request("POST", path, json=body) if body is not None else client.request("POST", path)
        p = r.json()
        return p if isinstance(p, dict) else {"Success": True, "Data": p}
    except Exception as exc:
        return {"Success": False, "ErrorName": None, "Error": None, "Data": [], "exception": str(exc)[:120]}


def items_len(payload: dict) -> int:
    data = payload.get("Data")
    if isinstance(data, dict) and isinstance(data.get("Items"), list):
        return len(data["Items"])
    if isinstance(data, list):
        return len(data)
    return 0


# ---- enumerate every live contract id with the sweep shape (50-id batches + a
# ---- sentinel so a dead range is a success answer, like sweep_enumerate does).
BATCH = 50
live: dict[int, dict[str, Any]] = {}
sentinel: int | None = None
start = 1
empty_run = 0
while start <= 2000:
    ids = list(range(start, start + BATCH))
    seed = sentinel if sentinel is not None and sentinel not in ids else None
    body = {"SearchIDs": [*ids, seed] if seed is not None else ids}
    p = call("/Contract/SearchByID", body)
    rows = p.get("Data") if isinstance(p.get("Data"), list) else []
    got = 0
    for row in rows:
        if isinstance(row, dict) and str(row.get("ID") or "").isdigit():
            ident = int(row["ID"])
            live[ident] = row
            if ident not in ids:
                continue
            got += 1
            if sentinel is None or ident > sentinel:
                sentinel = ident
    if not live:
        break
    empty_run = 0 if got else empty_run + 1
    if empty_run >= 4:
        break
    start += BATCH

print(f"contracts enumerated: {len(live)} (high id {max(live) if live else '-'})")

month_only = 0
line_only = 0
both = 0
neither = 0
exposed: list[dict[str, Any]] = []
spot_shapes: Counter = Counter()
item_keys: Counter = Counter()
samples: dict[str, Any] = {}

for ident, row in sorted(live.items()):
    months_payload = call("/Contract/GetContractDetailAnalysis", {"ID": ident, "id": ident, "RevenueDateType": 0})
    spots_payload = call("/Contract/GetSpotLineDetailAnalysis", {"id": ident, "ID": ident})
    load_payload = call(f"/Contract/Load/{ident}", {"name": "load"})

    months = normalize_revenue_months(months_payload)
    from_spots_endpoint = normalize_spot_lines(spots_payload)   # defect (A): always [] if Data.Items
    from_load = normalize_spot_lines(load_payload)
    raw_month_items = items_len(months_payload)
    raw_spot_items = items_len(spots_payload)

    # What the app assembles right now, through its own code path.
    assembled = normalize_contract(load_payload, [*(from_spots_endpoint or []), *(from_load or [])]) or {}
    final_lines = assembled.get("lines") or []

    if raw_spot_items:
        spot_shapes["spot-analysis has Data.Items"] += 1
        first = (spots_payload.get("Data") or {}).get("Items") or []
        if isinstance(first, list) and first and isinstance(first[0], dict):
            for key in sorted(first[0].keys()):
                item_keys[key] += 1
            samples.setdefault("spot_row", {k: first[0][k] for k in sorted(first[0])})
    else:
        spot_shapes["spot-analysis empty"] += 1

    has_months = bool(months)
    has_lines = bool(final_lines)
    if has_months and has_lines:
        both += 1
    elif has_months:
        month_only += 1
    elif has_lines:
        line_only += 1
    else:
        neither += 1

    # Exposure: nothing assembled, so every existing period for this contract is
    # "stale" — but the raw payloads say there IS data, or the contract is booked and
    # active with money on it. That is the delete case.
    sweep_net = row.get("NetAmount") or row.get("GrossAmount") or 0
    risky = bool(raw_month_items or raw_spot_items) and not has_months and not has_lines
    money_on_it = False
    try:
        money_on_it = float(sweep_net or 0) > 0
    except (TypeError, ValueError):
        money_on_it = False
    if risky or (money_on_it and not has_months and not has_lines):
        exposed.append(
            {
                "id": ident,
                "cd": row.get("ContractCD"),
                "status": row.get("Status"),
                "IsActiveFlag": row.get("IsActiveFlag"),
                "sweep_net": sweep_net,
                "raw_month_items": raw_month_items,
                "raw_spot_items": raw_spot_items,
                "assembled_lines": len(final_lines),
                "from_load": len(from_load),
                "months": len(months),
                "data_visible_to_parser": bool(raw_month_items or raw_spot_items),
            }
        )

print("\n=== what the tenant's revenue reads actually contain ===")
for key, val in spot_shapes.most_common():
    print(f"  {key}: {val}")
print(f"\n  assembled: months+lines={both} months-only={month_only} lines-only={line_only} NEITHER={neither}")

print("\n=== per-row keys on /Contract/GetSpotLineDetailAnalysis Data.Items (freq over contracts having any) ===")
for key, val in item_keys.most_common(30):
    print(f"  {key}: {val}")
if samples.get("spot_row"):
    print(f"  sample row: {json.dumps(samples['spot_row'], default=str)[:700]}")

print(f"\n=== EXPOSURE: {len(exposed)} contract(s) assemble to ZERO revenue lines ===")
for row in exposed[:40]:
    print(f"  {json.dumps(row, default=str)}")
hard = [r for r in exposed if r["data_visible_to_parser"]]
print(f"\n  of those, {len(hard)} have RAW DATA the parser could not read — these are the records")
print("  a certified run would prune away. "
      f"{len(exposed) - len(hard)} are genuinely empty in Aquira (pruning them is correct).")
try:
    client.delete("/Session/Delete")
except Exception:
    pass
client.close()
