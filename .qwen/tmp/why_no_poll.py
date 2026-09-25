"""Read-only: why is the 30-minute poll not firing?

app/jobs/poll.py skips a scheduled run whenever is_busy() is true, and is_busy()
calls active_job_count(), which counts work_queue rows in status 'queued' OR
'running'. claim_job() only ever selects status='queued' — so a row left in
'running' by a restarted worker is never reclaimed, and the poll then skips
forever. This prints exactly that state.
"""
from sqlalchemy import create_engine, text

from app.settings import get_settings

settings = get_settings()
engine = create_engine(settings.effective_database_url, future=True)

with engine.connect() as conn:
    print("=== work_queue by status ===")
    for row in conn.execute(text(
        "SELECT status, COUNT(*) FROM work_queue GROUP BY status ORDER BY status"
    )):
        print(f"  {row[0]:<10} {row[1]}")

    print("\n=== work_queue rows still queued or running (these block the poll) ===")
    rows = conn.execute(text(
        "SELECT id, kind, status, run_id, started_at, finished_at, left(error::text, 90) "
        "FROM work_queue WHERE status IN ('queued','running') ORDER BY id"
    )).fetchall()
    if not rows:
        print("  (none — the poll is NOT being blocked by the queue)")
    for r in rows:
        print(f"  job {r[0]} kind={r[1]} status={r[2]} run_id={r[3]} started={r[4]} finished={r[5]} err={r[6]}")

    print("\n=== 'worker is busy' poll skips, most recent ===")
    rows = conn.execute(text(
        "SELECT id, ts, message FROM job_event WHERE message LIKE '%worker is busy%' "
        "ORDER BY id DESC LIMIT 5"
    )).fetchall()
    if not rows:
        print("  (none recorded)")
    for r in rows:
        print(f"  [{r[0]}] {r[1]} {r[2]}")
    total = conn.execute(text(
        "SELECT COUNT(*) FROM job_event WHERE message LIKE '%worker is busy%'"
    )).scalar()
    print(f"  total such events: {total}")

    print("\n=== recent sync_run (trigger shows who started it) ===")
    for r in conn.execute(text(
        "SELECT id, started_at, finished_at, trigger, whatif, status FROM sync_run "
        "ORDER BY id DESC LIMIT 10"
    )):
        print(f"  [{r[0]}] {r[1]} -> {r[2]} trigger={r[3]} whatif={r[4]} status={r[5]}")

    print("\n=== settings that gate the poll ===")
    for r in conn.execute(text("SELECT key, left(value::text, 60) FROM app_settings ORDER BY key")):
        k = str(r[0]).lower()
        if any(t in k for t in ("interval", "whatif", "writeback", "sync", "calls", "environment")):
            print(f"  {r[0]} = {r[1]}")
    print(f"  env sync_interval_minutes={settings.sync_interval_minutes} whatif={settings.whatif} "
          f"role={settings.hubquira_role}")
