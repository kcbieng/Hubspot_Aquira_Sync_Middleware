import os

# Tests run in one process; they need the in-process worker to drain the queue.
os.environ.setdefault("HUBQUIRA_ROLE", "all")

import pytest

from app.settings import get_settings

get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_settings_singleton():
    """get_settings() is an lru_cache'd shared instance — a test that mutates
    it without monkeypatch (or whose monkeypatch ordering surprises a later
    module) must not leak into the rest of the session."""
    yield
    get_settings.cache_clear()
