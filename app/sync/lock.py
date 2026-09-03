from __future__ import annotations

from typing import Any


class SyncLock:
    def __init__(self):
        self._locked = False

    def acquire(self) -> None:
        if self._locked:
            raise RuntimeError("sync already running")
        self._locked = True

    def release(self) -> None:
        self._locked = False

    @property
    def is_locked(self) -> bool:
        return self._locked
