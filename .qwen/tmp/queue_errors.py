"""Read-only: what failed, and is a 'running' row orphaned right now?"""
from sqlalchemy import create_engine, text

from app.settings import get_settings

engine = create_engine(get_settings().effective_database_url, future=True)
with engine.connect() as conn:
    print("=== work_queue error rows ===")
    for r in conn.execute(text(
        "SELECT id, kind, status, run_id, started_at, finished_at, left(error, 200) "
        "FROM work_queue WHERE status='error' ORDER BY id"
    )):
        print(f"  job {r[0]} kind={r[1]} run={r[3]} started={r[4]} finished={r[5]}")
        print(f"      error: {r[6]}")

    print("\n=== reconcile events (what the DLQ pass said) ===")
    for r in conn.execute(text(
        "SELECT id, ts, level, message, left(payload_json::text, 220) FROM job_event "
        "WHERE job='reconcile' OR message LIKE '%reconcile%' ORDER BY id DESC LIMIT 8"
    )):
        print(f"  [{r[0]}] {r[1]} {r[2]}: {r[3]}")
        print(f"      {r[4]}")

    print("\n=== is the current run still advancing? ===")
    for r in conn.execute(text(
        "SELECT id, status, started_at, (SELECT COUNT(*) FROM sync_run_item i "
        "WHERE i.run_id = s.id) AS items FROM sync_run s WHERE id >= 9 ORDER BY id"
    )):
        print(f"  run {r[0]} status={r[1]} started={r[2]} items_recorded={r[3]}")
