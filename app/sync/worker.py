from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

from app.sync.orchestrator import SyncContext, SyncOrchestrator

logger = logging.getLogger(__name__)


@dataclass
class SyncJob:
    kind: str
    context: SyncContext | None = None
    run_id: int | None = None
    events: list[dict[str, Any]] | None = None
    extracted: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_jobs: queue.Queue[SyncJob | None] = queue.Queue()
_started = False
_busy = False
_lock = threading.Lock()
_thread: threading.Thread | None = None


def start_worker() -> None:
    global _started, _thread
    with _lock:
        if _started and _thread is not None and _thread.is_alive():
            return
        _started = True
        _thread = threading.Thread(target=_loop, daemon=True, name="hubquira-sync-worker")
        _thread.start()
        logger.info("HubQuira sync worker started")


def queue_size() -> int:
    return _jobs.qsize()


def is_busy() -> bool:
    return _busy or queue_size() > 0


def enqueue_sync(context: SyncContext) -> dict[str, Any]:
    start_worker()
    from app.db.repo import Repo

    repo = Repo()
    try:
        run = repo.add_run(context.trigger, context.whatif, status="queued")
        repo.add_event(
            "sync",
            "INFO",
            "sync queued",
            {
                "run_id": run.id,
                "trigger": context.trigger,
                "whatif": context.whatif,
                "entities": context.entities,
                "aquira_id": context.aquira_id,
                "queue": queue_size() + 1,
            },
        )
        run_id = run.id
    finally:
        repo.close()
    _jobs.put(SyncJob(kind="sync", context=context, run_id=run_id))
    return {
        "status": "queued",
        "run_id": run_id,
        "trigger": context.trigger,
        "whatif": context.whatif,
        "entities": context.entities,
        "aquira_id": context.aquira_id,
        "queue": queue_size(),
    }


def enqueue_hubspot_identity(events: list[dict[str, Any]]) -> dict[str, Any]:
    start_worker()
    _jobs.put(SyncJob(kind="hubspot-identity", events=events))
    return {"status": "queued", "events": len(events), "queue": queue_size()}


def enqueue_aquira_notification(extracted: dict[str, Any], *, whatif: bool) -> dict[str, Any]:
    start_worker()
    targets = list(extracted.get("ids") or extracted.get("contract_cds") or [])
    queued: list[dict[str, Any]] = []
    for ident in targets[:5]:
        queued.append(
            enqueue_sync(
                SyncContext(
                    trigger="aquira-webhook",
                    whatif=whatif,
                    entities=["companies", "contacts", "deals"],
                    aquira_id=str(ident),
                )
            )
        )
    if not queued:
        from app.db.repo import Repo

        repo = Repo()
        try:
            repo.add_event("webhook", "INFO", "Aquira notification had no contract id to sync", extracted)
        finally:
            repo.close()
    return {"status": "queued", "runs": queued, "processed": len(queued)}


def wait_for_run(run_id: int, timeout: float = 30.0) -> dict[str, Any] | None:
    import time

    from app.db.repo import Repo

    deadline = time.time() + timeout
    while time.time() < deadline:
        repo = Repo()
        try:
            run = repo.get_run(run_id)
            if run is not None and run.status not in {"queued", "running", "pending"}:
                return {
                    "status": run.status,
                    "run_id": run.id,
                    "whatif": run.whatif,
                    "trigger": run.trigger,
                    "error": run.error,
                    "summary_json": run.summary_json,
                }
        finally:
            repo.close()
        time.sleep(0.05)
    return None


def _loop() -> None:
    global _busy
    while True:
        job = _jobs.get()
        if job is None:
            _jobs.task_done()
            break
        _busy = True
        try:
            _execute(job)
        except Exception:
            logger.exception("Sync worker job failed (%s)", job.kind)
        finally:
            _busy = False
            _jobs.task_done()


def _execute(job: SyncJob) -> None:
    if job.kind == "sync" and job.context is not None:
        SyncOrchestrator().run(job.context, run_id=job.run_id)
        return
    if job.kind == "hubspot-identity":
        from app.webhooks.hubspot import process_hubspot_identity_events

        process_hubspot_identity_events(job.events or [])
