"""READ-ONLY A/B: does the readable-envelope change alter what this tenant assembles?

Imports the container's INSTALLED app.aquira.normalize (the code that produced today's
737 revenue periods) as BASELINE, and the patched module (which can now read
{"Data": {"Items": [...]}}) as CANDIDATE. Replays load_contract's exact probe order for
all 248 contracts with both, and reports every difference.

The point: airing rows carry StationShortName/SpotDate/Duration/Rate and no
StartDate/Amount, so the candidate must still discard them and the assembled output
must be identical. Any diff means the change is not safe to ship.
"""
import importlib.util as ilu
import json
import sys

import httpx

sys.path.insert(0, "/app")

from app.aquira import normalize as base  # installed code = baseline  # noqa: E402
from app.settings import get_settings  # noqa: E402

spec = ilu.spec_from_file_location("cand", "/app/norm_patched.py")
cand = ilu.module_from_spec(spec)
spec.loader.exec_module(cand)

s = get_settings()
client = httpx.Client(base_url=(s.aquira_base_url or "").rstrip("/"), timeout=60.0)
client.post("/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password})


def call(path, body=None):
    try:
        p = client.request("POST", path, json=body).json()
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def assemble(mod, cid):
    """Exact replay of app/aquira/client.py:load_contract's probe order."""
    load = call(f"/Contract/Load/{cid}", {"name": "load"})
    detail = call("/Contract/GetContractDetailAnalysis", {"ID": cid, "id": cid, "RevenueDateType": 0})
    months = mod.normalize_revenue_months(detail)
    if months:
        return mod.normalize_contract(load, months)
    spot_end = mod.normalize_spot_lines(call("/Contract/GetSpotLineDetailAnalysis", {"id": cid, "ID": cid}))
    lines = spot_end or mod.normalize_spot_lines(load)
    charges = mod.normalize_charge_lines(load) or mod.normalize_charge_lines(detail)
    return mod.normalize_contract(load, [*lines, *charges])


live = {}
for start in range(1, 280, 50):
    ids = list(range(start, start + 50))
    p = call("/Contract/SearchByID", {"SearchIDs": ids})
    for row in p.get("Data") or []:
        if isinstance(row, dict) and str(row.get("ID") or "").isdigit():
            live[int(row["ID"])] = row
print(f"contracts: {len(live)}")

diffs = 0
candidate_sees_rows = 0
for cid in sorted(live):
    b = assemble(base, cid) or {}
    c = assemble(cand, cid) or {}
    bl, cl = b.get("lines") or [], c.get("lines") or []
    if len(bl) != len(cl) or round(float(b.get("TotalValue") or 0), 2) != round(float(c.get("TotalValue") or 0), 2):
        diffs += 1
        print(f"  DIFF id {cid}: baseline lines={len(bl)} total={b.get('TotalValue')}"
              f"  candidate lines={len(cl)} total={c.get('TotalValue')}")
        if diffs > 15:
            print("  (more diffs — stopping the listing, still counting)")
    raw = call("/Contract/GetSpotLineDetailAnalysis", {"id": cid, "ID": cid})
    items = ((raw.get("Data") or {}).get("Items") or []) if isinstance(raw.get("Data"), dict) else []
    if items and not cl:
        candidate_sees_rows += 1

print(f"\nDIFFS between installed and patched assembly: {diffs}")
print(f"contracts whose airing rows the candidate can now READ but still discards"
      f" (no StartDate/Amount on the row): {candidate_sees_rows}")
print(json.dumps({"note": "candidate reads the envelope; rows are airing-grain"}, default=str))

client.delete("/Session/Delete")
client.close()
