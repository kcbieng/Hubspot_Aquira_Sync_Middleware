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
        job.reschedule("interval", minutes=interval_minutes)

    def run(self) -> dict[str, Any]:
        from app.db.repo import Repo

        settings = get_settings()
        repo = Repo()
        try:
            result = self.orchestrator.run(
                SyncContext(trigger="schedule", whatif=bool(settings.whatif)),
                repo=repo,
            )
            return result
        except Exception as exc:
            repo.add_event("poll", "ERROR", f"scheduled sync failed: {exc}")
            return {
                "status": "error",
                "message": str(exc),
                "timestamp": datetime.utcnow().isoformat(),
            }
