import sys
sys.path.insert(0, ".")
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import app.db as db_mod
from app.db.repo import Repo
import app.db.repo as repo_mod

engine = create_engine("sqlite:///:memory:", future=True)
db_mod.Base.metadata.create_all(engine)
repo_mod.SessionLocal = sessionmaker(bind=engine)
repo = Repo()
repo.add_dead_letter("deal", "49", "boom", None, attempts=0)
row = repo.list_dead_letters()[0]
print("row", row.id, row.attempts)

from app.sync.orchestrator import SyncContext
from app.sync.worker import enqueue_sync

try:
    r = enqueue_sync(SyncContext(trigger="manual", whatif=False, entities=["deals"], aquira_id="49"))
    print("queued:", r)
except Exception as exc:
    print("ENQUEUE RAISED:", type(exc).__name__, exc)

try:
    repo.bump_dead_letter(row.id)
    print("bumped ok")
except Exception as exc:
    print("BUMP RAISED:", type(exc).__name__, exc)
