from app.sync.orchestrator import SyncContext
from app.sync.worker import enqueue_sync, queue_size, wait_for_run


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
