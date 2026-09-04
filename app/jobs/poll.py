from __future__ import annotations

from datetime import datetime
from typing import Any

from app.db.repo import Repo
from app.settings import get_settings
from app.sync.orchestrator import SyncContext, SyncOrchestrator


class PollJob:
    def __init__(self, scheduler: Any | None = None):
        self.scheduler = scheduler

    def schedule(self, interval_minutes: int = 30) -> None:
        if self.scheduler is not None:
            self.scheduler.add_job(
                self.run,
                "interval",
                minutes=interval_minutes,
                next_run_time=datetime.utcnow(),
                replace_existing=True,
                id="sync_poll",
            )

    def run(self) -> dict[str, Any]:
        settings = get_settings()
        result = SyncOrchestrator().run(
            SyncContext(trigger="scheduled", whatif=settings.whatif, entities=["companies", "contacts", "deals"]),
            repo=Repo(),
        )
        return {
            "status": result["status"],
            "message": "poll cycle running",
            "whatif": result["whatif"],
            "entities": result["entities"],
            "timestamp": datetime.utcnow().isoformat(),
            "run_id": result["run_id"],
        }
