"""Run the WORKING TREE's Aquira pull against the live tenant, read-only.

Executed with PYTHONPATH pointing at a shadow copy of app/, so the deployed
container code stays untouched while the patched logic does a real load_catalog().
No HubSpot calls, no planning, no writes.
"""
from app.aquira.client import AquiraSessionClient
from app.settings import get_settings
from app.sync.planner import attach_revenue_summary

s = get_settings()
client = AquiraSessionClient(username=s.aquira_username, password=s.aquira_password)
catalog = client.load_catalog()
integrity = catalog["_integrity"]
contracts = catalog.get("contracts") or []

periods = 0
amount = 0.0
held = []
for row in contracts:
    if row.get("_detail_failed"):
        held.append(row.get("ID"))
    try:
        owned, _summary = attach_revenue_summary(row)
    except Exception:
        continue
    periods += len(owned)
    amount += sum(float(p.get("amount") or 0) for p in owned)

print("\n=== PATCHED PULL VERDICT ===")
for key in (
    "contract_rows",
    "revenue_rows",
    "client_rows",
    "failed_reads",
    "critical_reads",
    "absent_reads",
    "detail_failures",
    "certified",
):
    print(f"  {key:<16} {integrity.get(key)}")
print(f"  truncated_sources {integrity.get('truncated_sources')}")
print(f"  enumeration       {integrity.get('enumeration')}")
print(f"  contracts held from prune scope: {len(held)} {held[:20]}")
print(f"  revenue periods assembled: {periods}  total=${amount:,.2f}")
print("  (HubSpot holds 737 revenue_period records from the last run — compare.)")
for call in (integrity.get("failed_calls") or [])[:10]:
    print(f"    failed: {call.get('method')} {call.get('path')} shape={call.get('shape')} "
          f"http={call.get('http')} error_name={call.get('error_name')}")
