from app.sync.orchestrator import SyncContext
from datetime import datetime, timedelta

from app.db.models import SyncRun, WorkQueue
from app.db.repo import Repo
from app.sync.worker import enqueue_sync, queue_size, release_orphaned_jobs, wait_for_run


def _wedged_job(*, started_ago_minutes: int) -> tuple[int, int]:
    """A queue row left in 'running', as a killed or restarted worker leaves one."""
    repo = Repo()
    try:
        run = repo.add_run("schedule", True, status="running")
        job = repo.add_job("sync", {"trigger": "schedule", "whatif": True}, run_id=run.id)
        job.status = "running"
        job.started_at = datetime.utcnow() - timedelta(minutes=started_ago_minutes)
        repo.session.commit()
        return int(job.id), int(run.id)
    finally:
        repo.close()


def _job_and_run(job_id: int, run_id: int) -> tuple[WorkQueue, SyncRun]:
    repo = Repo()
    try:
        return repo.session.get(WorkQueue, job_id), repo.session.get(SyncRun, run_id)
    finally:
        repo.close()


def test_a_running_row_without_a_worker_is_released_when_the_loop_starts():
    # The wedge: is_busy() counts 'running', but claim_job() only takes 'queued', so one
    # interrupted run makes every scheduled poll skip as "worker is busy" forever.
    job_id, run_id = _wedged_job(started_ago_minutes=1)
    assert queue_size() >= 1
    assert release_orphaned_jobs() >= 1
    job, run = _job_and_run(job_id, run_id)
    assert job.status == "error"
    assert run.status == "error" and run.finished_at is not None


def test_the_stale_sweep_releases_an_abandoned_run_but_not_a_live_one():
    # Called from the poll tick, where a 'running' row may be a sync genuinely in
    # flight: only well past a real run's length can it be assumed dead.
    live_id, live_run = _wedged_job(started_ago_minutes=2)
    dead_id, dead_run = _wedged_job(started_ago_minutes=6 * 60)
    release_orphaned_jobs(stale_only=True)
    live_job, _live_run_row = _job_and_run(live_id, live_run)
    dead_job, dead_run_row = _job_and_run(dead_id, dead_run)
    assert live_job.status == "running", "a sync still being executed must not be cancelled"
    assert dead_job.status == "error"
    assert dead_run_row.status == "error"


def test_the_poll_arms_one_interval_from_the_schedulers_own_now():
    # The outage itself: schedule() handed the scheduler a naive datetime.utcnow(), and
    # APScheduler reads a naive next_run_time in the SCHEDULER'S timezone. Under
    # timezone="America/Chicago" the first poll armed ~5.5 hours out, and since every
    # container start re-armed it the same way, no scheduled sync ever ran.
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.jobs.poll import PollJob

    scheduler = BackgroundScheduler(timezone="America/Chicago")
    scheduler.start()
    try:
        PollJob(scheduler).schedule(30)
        job = scheduler.get_job("sync_poll")
        assert job is not None and job.next_run_time is not None
        minutes_out = (job.next_run_time - datetime.now(job.next_run_time.tzinfo)).total_seconds() / 60
        assert 25.0 <= minutes_out <= 31.0, f"first poll armed {minutes_out:.1f} minutes out, expected ~30"
    finally:
        scheduler.shutdown(wait=False)


def test_a_job_that_dies_before_the_run_starts_closes_the_run_record():
    # Reconcile once queued five retries while one held the lock; the four rejected jobs
    # left their run records 'queued' forever, reading as work still pending.
    repo = Repo()
    try:
        stuck = repo.add_run("reconcile", False, status="queued")
        finished = repo.add_run("reconcile", False, status="success")
        finished.status = "success"
        finished.finished_at = datetime.utcnow()
        repo.session.commit()
        stuck_id, finished_id = int(stuck.id), int(finished.id)

        assert repo.fail_run(stuck_id, "sync is already running") is True
        assert repo.fail_run(finished_id, "sync is already running") is False
        repo.session.expire_all()
        assert repo.session.get(SyncRun, stuck_id).status == "error"
        assert repo.session.get(SyncRun, stuck_id).error == "sync is already running"
        assert repo.session.get(SyncRun, finished_id).status == "success"
    finally:
        repo.close()


def test_enqueue_sync_returns_immediately_with_queued_run():
    result = enqueue_sync(SyncContext(trigger="test-queue", whatif=True, entities=["companies"]))
    assert result["status"] == "queued"
    assert result["run_id"]
    finished = wait_for_run(int(result["run_id"]), timeout=15)
    assert finished is not None
    assert finished["status"] in {"success", "partial", "error"}
    assert queue_size() >= 0


def test_web_role_does_not_run_sync_inline(monkeypatch):
    from app.settings import get_settings
    from app.sync import worker as worker_mod

    get_settings.cache_clear()
    monkeypatch.setenv("HUBQUIRA_ROLE", "web")
    get_settings.cache_clear()
    ran = {"count": 0}

    def fake_run(*args, **kwargs):
        ran["count"] += 1
        return None

    monkeypatch.setattr(worker_mod, "_execute_row", fake_run)
    result = enqueue_sync(SyncContext(trigger="web-only", whatif=True, entities=["companies"]))
    assert result["status"] == "queued"
    assert ran["count"] == 0
    get_settings.cache_clear()
