import os

# Tests run in one process; they need the in-process worker to drain the queue.
os.environ.setdefault("HUBQUIRA_ROLE", "all")

from app.settings import get_settings

get_settings.cache_clear()
