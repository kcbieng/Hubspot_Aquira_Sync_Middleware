"""Read-only: is the stack idle right now (safe to redeploy)?"""
from sqlalchemy import create_engine, text

from app.settings import get_settings

with create_engine(get_settings().effective_database_url, future=True).connect() as conn:
    print("=== work_queue by status ===")
    for r in conn.execute(text("SELECT status, COUNT(*) FROM work_queue GROUP BY status ORDER BY status")):
        print(f"  {r[0]:<8} {r[1]}")
    busy = conn.execute(text(
        "SELECT COUNT(*) FROM work_queue WHERE status IN ('queued','running')"
    )).scalar()
    print(f"\nclaimable-or-live rows: {busy}  (is_busy() -> {bool(busy)})")
    print("=== last run ===")
    for r in conn.execute(text(
        "SELECT id, started_at, finished_at, trigger, whatif, status FROM sync_run ORDER BY id DESC LIMIT 3"
    )):
        print(f"  [{r[0]}] {r[1]} -> {r[2]} trigger={r[3]} whatif={r[4]} status={r[5]}")
    print("=== newest events ===")
    for r in conn.execute(text(
        "SELECT id, ts, job, level, left(message, 150) FROM job_event ORDER BY id DESC LIMIT 6"
    )):
        print(f"  [{r[0]}] {r[1]} {r[2]}/{r[3]}: {r[4]}")
