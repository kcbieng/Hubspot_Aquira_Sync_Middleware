import pytest

from app.sync.lock import SyncLock


def test_lock_blocks_reentry():
    lock = SyncLock()
    lock.acquire()
    with pytest.raises(RuntimeError):
        lock.acquire()
    lock.release()
    assert lock.is_locked is False
