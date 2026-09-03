import pytest

from app.sync.whatif import SyncInProgress


def test_lock_and_sync_guard():
    with pytest.raises(SyncInProgress):
        raise SyncInProgress("sync already running")
