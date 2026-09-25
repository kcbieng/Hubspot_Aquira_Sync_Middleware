from __future__ import annotations

from typing import Any

from app.settings import get_settings
from app.sync.orchestrator import SyncContext, SyncOrchestrator


class PollJob:
    def __init__(self, scheduler: Any | None = None):
        self.scheduler = scheduler
        self.orchestrator = SyncOrchestrator()

    def schedule(self, interval_minutes: int = 30) -> None:
        if self.scheduler is not None:
            # Deliberately no next_run_time. APScheduler attaches the SCHEDULER'S
            # timezone to a naive value, and this project's scheduler runs in
            # settings.timezone: handing it `datetime.utcnow() + 30min` made the first
            # poll fire 30 minutes after the equivalent CENTRAL instant — 5.5 hours
            # late on this stack — and since every container start re-armed it the
            # same way, the scheduled sync never ran at all. Left to the trigger, the
            # first run is one interval from the scheduler's own now.
            self.scheduler.add_job(
                self.run,
                "interval",
                minutes=max(int(interval_minutes), 1),
                replace_existing=True,
                id="sync_poll",
            )

    def reschedule(self, interval_minutes: int) -> None:
        if self.scheduler is None:
            return
        job = self.scheduler.get_job("sync_poll")
        if job is None:
            self.schedule(interval_minutes)
            return
        job.reschedule("interval", minutes=max(int(interval_minutes), 1))

    def run(self) -> dict[str, Any]:
        from app.sync.worker import can_execute_jobs, enqueue_sync, queue_size, release_orphaned_jobs

        settings = get_settings()
        if can_execute_jobs():
            # The startup release only covers a worker that restarted; this covers the
            # live-process case where the worker thread died and left its row 'running'.
            # Without it, the queue never drains and every later poll skips as busy.
            release_orphaned_jobs(stale_only=True)
        depth = queue_size()
        if depth:
            from app.db.repo import Repo

            repo = Repo()
            try:
                repo.add_event(
                    "poll",
                    "INFO",
                    "scheduled sync skipped; worker is busy",
                    {"queued_or_running": depth},
                )
            finally:
                repo.close()
            return {"status": "skipped", "reason": "busy", "queue": depth}
        return enqueue_sync(SyncContext(trigger="schedule", whatif=bool(settings.whatif)))


_active_job: PollJob | None = None


def set_active_job(job: PollJob | None) -> None:
    global _active_job
    _active_job = job


def reschedule_active(interval_minutes: int) -> None:
    if _active_job is not None:
        _active_job.reschedule(interval_minutes)
