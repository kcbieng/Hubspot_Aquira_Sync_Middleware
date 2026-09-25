"""READ-ONLY: dump the full raw analysis rows for the 3 contracts that assembled to
zero revenue lines while the tenant returned data, plus one healthy contract as the
control. Decides whether normalize_* is dropping money or whether those rows are
airing-log detail that the line model cannot use.
"""
import json
import sys

import httpx

sys.path.insert(0, "/app")

from app.settings import get_settings  # noqa: E402

s = get_settings()
client = httpx.Client(base_url=(s.aquira_base_url or "").rstrip("/"), timeout=60.0)
client.post("/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password})

IDS = [int(a) for a in sys.argv[1:]] or [142, 99, 224, 1]

for cid in IDS:
    print(f"\n########## contract {cid} ##########")
    for label, path, body in (
        ("detail(months)", "/Contract/GetContractDetailAnalysis", {"ID": cid, "id": cid, "RevenueDateType": 0}),
        ("spot-analysis ", "/Contract/GetSpotLineDetailAnalysis", {"id": cid, "ID": cid}),
    ):
        p = client.request("POST", path, json=body).json()
        items = ((p.get("Data") or {}).get("Items") or []) if isinstance(p.get("Data"), dict) else []
        print(f"  {label}: Success={p.get('Success')} items={len(items)}")
        for row in items[:3]:
            print(f"     {json.dumps(row, default=str)}")

print("\n=== money fields present anywhere in the spot-analysis rows? ===")
# Scan every contract's rows for any field that carries money or a flight window.
found: dict[str, set] = {}
alive = set(range(1, 280))
for start in range(1, 280, 50):
    ids = list(range(start, start + 50))
    batch = client.request("POST", "/Contract/SearchByID", json={"SearchIDs": ids}).json()
    rows = batch.get("Data") or []
    for row in rows if isinstance(rows, list) else []:
        cid = row.get("ID")
        if not cid:
            continue
        p = client.request("POST", "/Contract/GetSpotLineDetailAnalysis", json={"id": cid, "ID": cid}).json()
        items = ((p.get("Data") or {}).get("Items") or []) if isinstance(p.get("Data"), dict) else []
        for item in items:
            for key, val in item.items():
                low = key.lower()
                if any(tok in low for tok in ("amount", "start", "end", "rate", "total", "price", "cost", "charge")):
                    if val not in (None, 0, 0.0, "", [], {}):
                        found.setdefault(key, set()).add(str(val)[:40])
print("  non-zero money/window keys seen across the whole tenant's spot-analysis rows:")
for key, vals in sorted(found.items()):
    print(f"    {key}: {len(vals)} distinct non-zero values, e.g. {sorted(vals)[:4]}")
if not found:
    print("    (none — no Amount/StartDate/EndDate/Rate is ever populated)")

client.delete("/Session/Delete")
client.close()
