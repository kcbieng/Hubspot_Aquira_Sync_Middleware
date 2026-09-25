"""Decisive check of the APScheduler assumption behind the fix:
does a NAIVE next_run_time get read in the scheduler's timezone?
No app code touched, no DB, no tenant calls.
"""
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

sched = BackgroundScheduler(timezone="America/Chicago")
sched.start()
try:
    sched.add_job(
        lambda: None, "interval", minutes=30, id="old",
        next_run_time=datetime.utcnow() + timedelta(minutes=30),
    )
    sched.add_job(lambda: None, "interval", minutes=30, id="new")
    for job_id in ("old", "new"):
        job = sched.get_job(job_id)
        minutes = (job.next_run_time - datetime.now(job.next_run_time.tzinfo)).total_seconds() / 60
        print(f"  {job_id}: next_run_time={job.next_run_time}  -> armed {minutes:.1f} min out")
finally:
    sched.shutdown(wait=False)
