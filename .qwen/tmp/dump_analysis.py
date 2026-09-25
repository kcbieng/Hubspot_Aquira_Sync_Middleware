"""Read-only: what do the two analysis envelopes actually contain?

normalize_spot_lines() accepts a bag only when isinstance(bag, list), and this
tenant answers those endpoints with Data as a DICT — so it may be reading nothing.
normalize_revenue_months() digs Data.Items, which is why months work. Print the
real structure for a contract that has revenue so the difference is visible.
"""
import json
import sys

import httpx

sys.path.insert(0, "/app")

from app.settings import get_settings  # noqa: E402

s = get_settings()
client = httpx.Client(base_url=(s.aquira_base_url or "").rstrip("/"), timeout=45.0)
client.post("/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password})


def walk(label, path, body):
    p = client.request("POST", path, json=body).json()
    data = p.get("Data")
    print(f"\n{label}  {path}")
    print(f"  Success={p.get('Success')} ErrorName={p.get('ErrorName')!r} Error={p.get('Error')}")
    if isinstance(data, dict):
        print(f"  Data keys={sorted(data.keys())}")
        for key, val in data.items():
            if isinstance(val, list):
                print(f"    Data['{key}'] list len={len(val)}")
                if val:
                    first = val[0]
                    if isinstance(first, dict):
                        print(f"      [0] keys={sorted(first.keys())[:30]}")
                        print(f"      [0] = {json.dumps(first, default=str)[:900]}")
                    else:
                        print(f"      [0] = {json.dumps(first, default=str)[:200]}")
            else:
                print(f"    Data['{key}'] = {json.dumps(val, default=str)[:200]}")
    else:
        print(f"  Data={json.dumps(data, default=str)[:400]}  type={type(data).__name__}")


for cid in (1, 8):
    print(f"########## contract {cid} ##########")
    walk("detail-analysis", "/Contract/GetContractDetailAnalysis", {"ID": cid, "id": cid, "RevenueDateType": 0})
    walk("spot-analysis", "/Contract/GetSpotLineDetailAnalysis", {"id": cid, "ID": cid})

client.delete("/Session/Delete")
client.close()
