"""Read-only: prove (or kill) the scheduling hypothesis for the 30-minute poll.

app/jobs/poll.py schedules the interval job with
    next_run_time=datetime.utcnow() + timedelta(minutes=interval)
while the scheduler itself is built as BackgroundScheduler(timezone=settings.timezone).
APScheduler attaches the SCHEDULER'S timezone to a naive next_run_time, so a UTC
wall-clock handed to a US/Central scheduler reads as Central time — i.e. the first
run is due hours late, not 30 minutes. Every container restart re-arms it, so a stack
that restarts more often than the offset never polls at all.

No tenant traffic: it only builds a scheduler in-process and reads the DB.
"""
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, text

from app.jobs.poll import PollJob
from app.settings import get_settings

settings = get_settings()
print(f"settings.timezone          = {settings.timezone!r}")
print(f"settings.sync_interval_minutes = {settings.sync_interval_minutes}")
print(f"settings.hubquira_role       = {settings.hubquira_role!r}")
print(f"settings.whatif              = {settings.whatif}")
print(f"real now (UTC)               = {datetime.now(timezone.utc)}")
try:
    from zoneinfo import ZoneInfo

    print(f"real now (scheduler-local)   = {datetime.now(ZoneInfo(str(settings.timezone)))}")
except Exception as exc:
    print(f"real now (scheduler-local)   = <{type(exc).__name__}: {exc}>")

scheduler = BackgroundScheduler(timezone=settings.timezone)
scheduler.start()
PollJob(scheduler).schedule(int(settings.sync_interval_minutes))
job = scheduler.get_job("sync_poll")
print(f"\nnext_run_time as computed by the real code path = {job.next_run_time}")
print(f"  hours from now = {(job.next_run_time - datetime.now(job.next_run_time.tzinfo)).total_seconds() / 3600:.2f}")
scheduler.shutdown(wait=False)

engine = create_engine(settings.effective_database_url, future=True)
with engine.connect() as conn:
    print("\n=== has the poll job EVER run? (job_event) ===")
    rows = conn.execute(text(
        "SELECT job, COUNT(*) FROM job_event GROUP BY job ORDER BY 2 DESC"
    )).fetchall()
    for r in rows:
        print(f"  job={r[0]:<12} events={r[1]}")
    runs = conn.execute(text(
        "SELECT trigger, status, COUNT(*) FROM sync_run GROUP BY trigger, status ORDER BY 3 DESC"
    )).fetchall()
    print("\n=== sync_run by trigger+status ===")
    for r in runs:
        print(f"  trigger={r[0]:<12} status={r[1]:<8} runs={r[2]}")
    print("\n=== the live run 10 and its job row ===")
    for r in conn.execute(text(
        "SELECT id, started_at, finished_at, status FROM sync_run WHERE id=10"
    )):
        print(f"  sync_run 10: started={r[1]} finished={r[2]} status={r[3]}")
    for r in conn.execute(text(
        "SELECT id, status, started_at, finished_at FROM work_queue WHERE id=10"
    )):
        print(f"  work_queue 10: status={r[1]} started={r[2]} finished={r[3]}")
