from __future__ import annotations

from datetime import datetime
from typing import Any


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

    def run(self) -> dict[str, str]:
        return {"status": "ok", "message": "poll cycle running", "timestamp": datetime.utcnow().isoformat()}
