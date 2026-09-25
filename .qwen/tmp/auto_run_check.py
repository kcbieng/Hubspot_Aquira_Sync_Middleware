"""Read-only: what will the FIRST automatic scheduled run actually do?

Three separate fixes land together here, and their combination is what matters:
  1. the poll now arms 30 minutes out instead of 5.5 hours (deployed: b26407d);
  2. the pull can now certify (no more LoadSpotline failure);
  3. certification is what switches on revenue-period pruning AND deal archiving.
The scheduled run inherits settings.whatif from the settings table, so if whatif is
persisted off, the first unattended automatic run is also the first live pruning run.
"""
from sqlalchemy import create_engine, text

from app.settings import get_settings

engine = create_engine(get_settings().effective_database_url, future=True)
with engine.connect() as conn:
    print("=== settings that decide it ===")
    for r in conn.execute(text("SELECT key, value_enc FROM app_settings ORDER BY key")):
        if any(t in str(r[0]).lower() for t in ("whatif", "writeback", "sync_calls", "cf_access", "sso", "interval")):
            print(f"  {r[0]} = {r[1]}")

    print("\n=== every run so far: trigger / whatif / status ===")
    for r in conn.execute(text("SELECT id, trigger, whatif, status, started_at FROM sync_run ORDER BY id")):
        print(f"  [{r[0]}] trigger={str(r[1]):<10} whatif={str(r[2]):<5} status={r[3]:<8} {r[4]}")

    print("\n=== last run's planned items by type/action ===")
    for r in conn.execute(text(
        "SELECT entity_type, action, COUNT(*) FROM sync_run_item "
        "WHERE run_id = (SELECT MAX(id) FROM sync_run) GROUP BY entity_type, action ORDER BY 3 DESC"
    )):
        print(f"  {r[0]:<16} {r[1]:<14} {r[2]}")

    print("\n=== dead letters still open (reconcile will keep re-triggering these) ===")
    for r in conn.execute(text("SELECT COUNT(*) FROM dead_letter WHERE resolved_at IS NULL")):
        print(f"  open rows: {r[0]}")
