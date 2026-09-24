"""The default scheduled jobs, registered identically from the web lifespan
and the worker process entry point — one list to edit, no drift between the
two copies."""
from __future__ import annotations

from typing import Any


def register_default_jobs(scheduler: Any, settings: Any) -> None:
    from app.jobs.poll import PollJob, set_active_job
    from app.jobs.reconcile import schedule_reconciliation
    from app.notify import run_match_digest

    poll_job = PollJob(scheduler)
    poll_job.schedule(settings.sync_interval_minutes)
    set_active_job(poll_job)
    schedule_reconciliation(scheduler)
    scheduler.add_job(run_match_digest, "cron", hour=7, minute=5, replace_existing=True, id="match_digest")
