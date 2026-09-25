from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from app.settings import get_settings
from app.sync.orchestrator import SyncContext

logger = logging.getLogger(__name__)

_started = False
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()


def _role() -> str:
    return str(getattr(get_settings(), "hubquira_role", "all") or "all").strip().lower()


def can_execute_jobs() -> bool:
    return _role() in {"worker", "all"}


def start_worker() -> None:
    global _started, _thread
    if not can_execute_jobs():
        return
    with _lock:
        if _started and _thread is not None and _thread.is_alive():
            return
        _started = True
        _stop.clear()
        _thread = threading.Thread(target=run_forever, kwargs={"once": False}, daemon=True, name="hubquira-sync-worker")
        _thread.start()
        logger.info("HubQuira sync worker loop started (role=%s)", _role())


def queue_size() -> int:
    from app.db.repo import Repo

    repo = Repo()
    try:
        return repo.active_job_count()
    except Exception:
        return 0
    finally:
        repo.close()


def is_busy() -> bool:
    return queue_size() > 0


# A live full run of the deployed tenant took 30 minutes, so a row still 'running'
# well past that belongs to a process that no longer exists.
RUN_STALE_AFTER_MINUTES = 90


def release_orphaned_jobs(*, stale_only: bool = False) -> int:
    """Clear 'running' queue rows that no live worker owns, and fail their run records.

    Two separate situations, both of which otherwise silence automatic syncing forever:

    * the loop is starting, so anything marked 'running' was left by a previous
      incarnation of this process (``stale_only=False``) — it died mid-run, or was
      restarted;
    * a row has been 'running' far longer than a real run takes (``stale_only=True``),
      for the case where the loop is alive but its worker thread is not.

    Only ever call this from the process that executes jobs: from the web container a
    'running' row is a sync the worker is genuinely doing, and releasing it would
    cancel live work and write a false error over it.
    """
    from app.db.repo import Repo

    repo = Repo()
    try:
        jobs = repo.running_jobs()
        if stale_only:
            # started_at is written with datetime.utcnow(), so compare naive-to-naive:
            # .timestamp() would reinterpret it as machine-local time and reintroduce
            # the very offset bug this function exists to work around.
            cutoff = datetime.utcnow() - timedelta(minutes=RUN_STALE_AFTER_MINUTES)
            jobs = [job for job in jobs if job.started_at is None or job.started_at.replace(tzinfo=None) < cutoff]
        if not jobs:
            return 0
        count = repo.release_orphaned_jobs(
            jobs,
            "no live worker held this job — released so the queue and the schedule can move",
        )
        if count:
            repo.add_event(
                "sync",
                "WARN",
                f"released {count} orphaned sync job(s) left 'running' without a worker",
                {"jobs": [int(job.id) for job in jobs], "runs": [job.run_id for job in jobs]},
            )
        return count
    except Exception:
        logger.exception("could not release orphaned sync jobs")
        return 0
    finally:
        repo.close()


def enqueue_sync(context: SyncContext) -> dict[str, Any]:
    if can_execute_jobs():
        start_worker()
    from app.db.repo import Repo

    repo = Repo()
    try:
        run = repo.add_run(context.trigger, context.whatif, status="queued")
        payload = {
            "kind": "sync",
            "trigger": context.trigger,
            "whatif": context.whatif,
            "entities": context.entities,
            "aquira_id": context.aquira_id,
        }
        repo.add_job("sync", payload, run_id=run.id)
        repo.add_event(
            "sync",
            "INFO",
            "sync queued",
            {"run_id": run.id, "trigger": context.trigger, "whatif": context.whatif, "aquira_id": context.aquira_id},
        )
        run_id = run.id
        depth = repo.active_job_count()
    finally:
        repo.close()
    return {
        "status": "queued",
        "run_id": run_id,
        "trigger": context.trigger,
        "whatif": context.whatif,
        "entities": context.entities,
        "aquira_id": context.aquira_id,
        "queue": depth,
    }


def enqueue_hubspot_identity(events: list[dict[str, Any]]) -> dict[str, Any]:
    if _role() != "web":
        start_worker()
    from app.db.repo import Repo

    repo = Repo()
    try:
        repo.add_job("hubspot-identity", {"kind": "hubspot-identity", "events": events})
        depth = repo.active_job_count()
    finally:
        repo.close()
    return {"status": "queued", "events": len(events), "queue": depth}


def enqueue_aquira_notification(extracted: dict[str, Any], *, whatif: bool) -> dict[str, Any]:
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


def _execute_row(kind: str, payload: dict[str, Any], run_id: int | None) -> None:
    if not can_execute_jobs():
        raise RuntimeError(f"refusing to execute {kind} in HubQuira role={_role()}")
    if kind == "sync":
        from app.sync.orchestrator import SyncOrchestrator

        SyncOrchestrator().run(
            SyncContext(
                trigger=str(payload.get("trigger") or "manual"),
                whatif=bool(payload.get("whatif", True)),
                entities=payload.get("entities"),
                aquira_id=payload.get("aquira_id"),
            ),
            run_id=run_id,
        )
        return
    if kind == "hubspot-identity":
        from app.webhooks.hubspot import process_hubspot_identity_events

        process_hubspot_identity_events(payload.get("events") or [])


def run_forever(*, once: bool = False, idle_sleep: float = 0.4) -> None:
    from app.db.repo import Repo

    if can_execute_jobs():
        # Nothing of ours is in flight yet, so a 'running' row here was left by the
        # previous incarnation of this process — usually a restart during a live run.
        release_orphaned_jobs()
    while not _stop.is_set():
        repo = Repo()
        try:
            job = repo.claim_job()
            job_id = job.id if job else None
            kind = job.kind if job else None
            run_id = job.run_id if job else None
            raw = job.payload_json if job else None
        finally:
            repo.close()
        if job_id is None:
            if once:
                return
            time.sleep(idle_sleep)
            continue
        payload: dict[str, Any] = {}
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    payload = loaded
            except json.JSONDecodeError:
                payload = {}
        error = None
        try:
            _execute_row(kind or "sync", payload, run_id)
            status = "done"
        except Exception as exc:
            logger.exception("Sync worker job %s failed", job_id)
            error = str(exc)
            status = "error"
        done = Repo()
        try:
            done.finish_job(job_id, status=status, error=error)
            if error and run_id is not None:
                # A job that died before the orchestrator could own its run record
                # leaves that row 'queued' forever; the UI then shows work pending that
                # will never arrive.
                try:
                    done.fail_run(int(run_id), error[:300])
                except (TypeError, ValueError):
                    logger.warning("job %s carried an unusable run_id %r", job_id, run_id)
        finally:
            done.close()
        if once:
            return


def main() -> None:
    import os

    os.environ["HUBQUIRA_ROLE"] = "worker"
    get_settings.cache_clear()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.runtime import apply_db_overlay

    apply_db_overlay()
    settings = get_settings()
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.jobs.poll import set_active_job
    from app.jobs.setup import register_default_jobs

    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.start()
    register_default_jobs(scheduler, settings)
    logger.info("HubQuira worker process ready (role=%s, poll every %s min)", _role(), settings.sync_interval_minutes)
    try:
        run_forever()
    finally:
        set_active_job(None)
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
