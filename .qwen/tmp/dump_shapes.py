"""Read-only: dump the RAW revenue envelopes for line-less contracts.

The census proved the shape of the *failures* (all no-rows) but not whether
GetSpotLineDetailAnalysis / GetContractDetailAnalysis return zero rows or rows
that normalize_spot_lines cannot map. This prints the raw payload so the two are
distinguishable, plus what the app's own normalizers made of it.
"""
import json
import sys

import httpx

sys.path.insert(0, "/app")

from app.aquira.normalize import (  # noqa: E402
    normalize_charge_lines,
    normalize_revenue_months,
    normalize_spot_lines,
)
from app.settings import get_settings  # noqa: E402

IDS = [int(a) for a in sys.argv[1:]] or [8, 36, 65]

s = get_settings()
base = (s.aquira_base_url or "").rstrip("/")
client = httpx.Client(base_url=base, timeout=45.0)
login = client.post("/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password}).json()
print("login ok:", bool(login.get("Success", True)))


def show(label, path, body, normalizer):
    r = client.request("POST", path, json=body)
    try:
        p = r.json()
    except Exception:
        print(f"    {label}: http={r.status_code} non-JSON")
        return
    env = {k: v for k, v in p.items() if k not in ("Data", "Entity")} if isinstance(p, dict) else p
    print(f"    {label} {path} http={r.status_code} envelope={json.dumps(env, default=str)[:200]}")
    data = p.get("Data") if isinstance(p, dict) else None
    ent = p.get("Entity") if isinstance(p, dict) else None
    print(f"      Data type={type(data).__name__} len={len(data) if isinstance(data, list) else '-'}"
          f"  Entity type={type(ent).__name__}")
    if isinstance(data, list) and data:
        print(f"      row[0] keys={sorted(data[0].keys())[:24] if isinstance(data[0], dict) else repr(data[0])[:120]}")
        print(f"      row[0] = {json.dumps(data[0], default=str)[:600]}")
    elif isinstance(ent, dict) and ent:
        print(f"      Entity keys={sorted(ent.keys())[:24]}")
        print(f"      Entity = {json.dumps(ent, default=str)[:600]}")
    print(f"      -> normalize => months={len(normalize_revenue_months(p))} "
          f"spot={len(normalize_spot_lines(p))} charges={len(normalize_charge_lines(p))}")


for ident in IDS:
    print(f"\n=== contract {ident} ===")
    show("months ", "/Contract/GetContractDetailAnalysis", {"ID": ident, "id": ident, "RevenueDateType": 0}, None)
    show("spots  ", "/Contract/GetSpotLineDetailAnalysis", {"id": ident, "ID": ident}, None)
    show("loadspot", "/Contract/LoadSpotline", {"ContractID": ident}, None)
    show("load   ", f"/Contract/Load/{ident}", {"name": "load"}, None)

try:
    client.delete("/Session/Delete")
except Exception:
    pass
client.close()
