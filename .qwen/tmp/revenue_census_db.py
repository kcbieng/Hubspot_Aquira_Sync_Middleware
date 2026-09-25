"""Read-only: how much revenue_period data does the last live run actually carry?

Bounds the blast radius of the prune path: the periods that exist are the ones a
certified run can delete.
"""
import sqlite3  # noqa: F401  (only for the driver choice; we use SQLAlchemy)

from sqlalchemy import create_engine, text

from app.settings import get_settings

engine = create_engine(get_settings().effective_database_url, future=True)
with engine.connect() as conn:
    print("=== sync_run_item for the last runs, by entity_type + action ===")
    rows = conn.execute(
        text(
            "SELECT run_id, entity_type, action, COUNT(*) AS n "
            "FROM sync_run_item WHERE run_id IN (SELECT id FROM sync_run ORDER BY id DESC LIMIT 3) "
            "GROUP BY run_id, entity_type, action ORDER BY run_id DESC, n DESC"
        )
    ).fetchall()
    for r in rows:
        print(f"  run {r[0]:>4} {str(r[1]):<16} {str(r[2]):<14} {r[3]}")

    print("\n=== distinct contracts that produced revenue_period items (last run) ===")
    rows = conn.execute(
        text(
            "SELECT COUNT(DISTINCT regexp_replace(aquira_id, ':[0-9-]*:[0-9]*$', '')) "
            "FROM sync_run_item WHERE run_id = (SELECT MAX(id) FROM sync_run) "
            "AND entity_type='revenue_period'"
        )
    ).fetchall()
    for r in rows:
        print(f"  contracts with revenue periods: {r[0]}")

    print("\n=== sample revenue_period aquira_ids (contract:period:station) ===")
    rows = conn.execute(
        text(
            "SELECT aquira_id, action, left(diff_json::text, 120) FROM sync_run_item "
            "WHERE run_id = (SELECT MAX(id) FROM sync_run) AND entity_type='revenue_period' "
            "ORDER BY id DESC LIMIT 8"
        )
    ).fetchall()
    for r in rows:
        print(f"  {r[0]:<28} {r[1]:<10} {r[2]}")
