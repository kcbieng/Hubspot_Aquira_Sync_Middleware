"""Read-only: will the FIRST automatic run be a live, pruning run?

Three recently-fixed things meet here: the poll now actually fires, the pull can now
certify, and the scheduled run inherits settings.whatif from the DB overlay.
"""
from sqlalchemy import create_engine, text

from app.settings import get_settings

engine = create_engine(get_settings().effective_database_url, future=True)
with engine.connect() as conn:
    cols = [r[1] for r in conn.execute(text("SELECT * FROM app_settings LIMIT 0")).keys()] if False else None
    print("=== app_settings (auth/pruning relevant) ===")
    for r in conn.execute(text("SELECT key, value FROM app_settings ORDER BY key")):
        key = str(r[0]).lower()
        if any(t in key for t in ("whatif", "writeback", "sync_calls", "sso", "oidc", "cf_access", "interval", "create")):
            print(f"  {r[0]} = {r[1]}")

    print("\n=== every run: trigger / whatif / status ===")
    for r in conn.execute(text(
        "SELECT id, trigger, whatif, status, started_at FROM sync_run ORDER BY id"
    )):
        print(f"  [{r[0]}] trigger={r[1]:<10} whatif={str(r[2]):<5} status={r[3]:<8} {r[4]}")

    print("\n=== how much destructive work is queued up? ===")
    for r in conn.execute(text(
        "SELECT entity_type, action, COUNT(*) FROM sync_run_item "
        "WHERE run_id = (SELECT MAX(id) FROM sync_run) GROUP BY entity_type, action ORDER BY 3 DESC"
    )):
        print(f"  {r[0]:<16} {r[1]:<14} {r[2]}")
    print("  (note: no delete-stale/archive rows can exist yet — pruning was suppressed)")
