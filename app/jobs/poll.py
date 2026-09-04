from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.settings import get_settings
from app.sync.orchestrator import SyncContext, SyncOrchestrator


class PollJob:
    def __init__(self, scheduler: Any | None = None):
        self.scheduler = scheduler
        self.orchestrator = SyncOrchestrator()

    def schedule(self, interval_minutes: int = 30) -> None:
        if self.scheduler is not None:
            self.scheduler.add_job(
                self.run,
                "interval",
                minutes=interval_minutes,
                next_run_time=datetime.utcnow() + timedelta(minutes=max(interval_minutes, 1)),
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
        from app.sync.worker import enqueue_sync, is_busy

        settings = get_settings()
        if is_busy():
            from app.db.repo import Repo

            repo = Repo()
            try:
                repo.add_event("poll", "INFO", "scheduled sync skipped; worker is busy")
            finally:
                repo.close()
            return {"status": "skipped", "reason": "busy"}
        return enqueue_sync(SyncContext(trigger="schedule", whatif=bool(settings.whatif)))


_active_job: PollJob | None = None


def set_active_job(job: PollJob | None) -> None:
    global _active_job
    _active_job = job


def reschedule_active(interval_minutes: int) -> None:
    if _active_job is not None:
        _active_job.reschedule(interval_minutes)
