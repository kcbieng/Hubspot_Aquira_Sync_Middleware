"""READ-ONLY end-to-end proof: run the PATCHED Aquira pull over the whole tenant and
report the integrity verdict — no HubSpot writes, no planning, no apply.

This is the gate for turning the first certified run on. It must show:
  * truncated_sources empty   (the row-cap sentinel must not mistake a 657-row spot
                               log for a capped enumeration — that would re-block
                               certification with a brand-new false positive)
  * critical_reads empty      (no real read damage)
  * revenue_rows > 0          (the all-empty backstop passes)
  * certified True
and it prints the assembled revenue totals so they can be compared with the 737
revenue_period records HubSpot already holds.
"""
import importlib.util as ilu
import json
import sys

sys.path.insert(0, "/app")

from app.aquira.client import AquiraSessionClient  # noqa: E402
from app.settings import get_settings  # noqa: E402

# Load the patched modules under new names so the running container's own code stays
# untouched while we exercise the working tree's logic.
for name, path in (("norm_p", "/app/norm_patched.py"), ("cli_p", "/app/client_patched.py")):
    spec = ilu.spec_from_file_location(name, path)
    mod = ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)

if "--baseline" in sys.argv:
    from app.aquira.client import AquiraSessionClient as Used
    print("### BASELINE (container code as deployed)")
else:
    Used = sys.modules["cli_p"].AquiraSessionClient
    print("### CANDIDATE (working-tree code)")

s = get_settings()
client = Used(username=s.aquira_username, password=s.aquira_password)
catalog = client.load_catalog()
integrity = catalog["_integrity"]

contracts = catalog.get("contracts") or []
with_lines = [c for c in contracts if (c.get("lines") or [])]
total = sum(float(c.get("TotalValue") or 0) for c in contracts)
period_total = 0.0
period_count = 0
for c in contracts:
    try:
        periods, _summary = sys.modules["app.sync.planner"].attach_revenue_summary(c)
    except Exception:
        continue
    period_count += len(periods)
    period_total += sum(float(p.get("amount") or 0) for p in periods)

print(json.dumps(
    {
        "clients": len(catalog.get("clients") or []),
        "contacts": len(catalog.get("contacts") or []),
        "reps": len(catalog.get("reps") or []),
        "contract_rows": integrity.get("contract_rows"),
        "contracts_with_lines": len(with_lines),
        "revenue_rows": integrity.get("revenue_rows"),
        "periods_assembled": period_count,
        "period_amount_total": round(period_total, 2),
        "contract_total_value": round(total, 2),
        "failed_reads": integrity.get("failed_reads"),
        "critical_reads": integrity.get("critical_reads"),
        "absent_reads": integrity.get("absent_reads"),
        "detail_failures": integrity.get("detail_failures"),
        "truncated_sources": integrity.get("truncated_sources"),
        "certified": integrity.get("certified"),
    },
    indent=2, default=str,
))
print("\nfirst failed calls (if any):")
for call in (integrity.get("failed_calls") or [])[:6]:
    print("  ", json.dumps(call, default=str)[:220])
