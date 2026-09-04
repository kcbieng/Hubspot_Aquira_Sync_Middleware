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
