"""Read-only: pull the live run's 'Revenue pruning suppressed' events and their payloads.

Runs INSIDE the stack container (it already has DATABASE_URL). Prints the stored
failed_calls, which is the only thing that can name which reads were counted.
"""
from sqlalchemy import create_engine, text

from app.settings import get_settings

engine = create_engine(get_settings().effective_database_url, future=True)

with engine.connect() as conn:
    print("=== latest sync WARN events ===")
    rows = conn.execute(
        text(
            "SELECT id, ts, message, payload_json::text FROM job_event "
            "WHERE job='sync' AND level='WARN' ORDER BY id DESC LIMIT 6"
        )
    ).fetchall()
    for rid, ts, message, payload in rows:
        print("-" * 72)
        print(f"[{rid}] {ts}")
        print(f"  message: {message}")
        print(f"  payload: {payload}")

    print("\n=== latest sync_run summaries ===")
    runs = conn.execute(
        text(
            "SELECT id, started_at, finished_at, trigger AS trg, whatif, status, "
            "left(summary_json::text, 900) FROM sync_run ORDER BY id DESC LIMIT 6"
        )
    ).fetchall()
    for r in runs:
        print(f"[{r[0]}] {r[1]} -> {r[2]} trigger={r[3]} whatif={r[4]} status={r[5]}")
        print(f"      {r[6]}")
