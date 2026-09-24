"""Teams alerting (best-effort by construction) and the dead-letter cockpit."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.alerts as alerts
import app.db as db_mod
from app.db.models import WorkQueue
from app.db.repo import Repo
from app.main import app
from app.sync.orchestrator import SyncContext, SyncOrchestrator


# ---------------------------------------------------------------------------
# notify_teams transport
# ---------------------------------------------------------------------------
def test_notify_teams_posts_workflows_card(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "get_settings", lambda: SimpleNamespace(teams_webhook_url="https://teams.test/hook"))

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))

        class R:
            status_code = 200

        return R()

    monkeypatch.setattr(alerts.httpx, "post", fake_post)
    assert alerts.notify_teams("hello") is True
    url, payload = calls[0]
    assert url == "https://teams.test/hook"
    assert payload["attachments"][0]["content"]["body"][0]["text"] == "hello"
    assert payload["text"] == "hello"  # legacy connector fallback travels along


def test_notify_teams_is_silent_or_broken_never_loud(monkeypatch):
    monkeypatch.setattr(alerts, "get_settings", lambda: SimpleNamespace(teams_webhook_url=""))
    assert alerts.notify_teams("x") is False
    monkeypatch.setattr(alerts, "get_settings", lambda: SimpleNamespace(teams_webhook_url="https://teams.test/hook"))

    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(alerts.httpx, "post", boom)
    assert alerts.notify_teams("x") is False  # never raises into the run


def test_report_run_alerts_only_when_notable(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "notify_teams", lambda text: sent.append(text) or True)
    assert alerts.report_run(status="success") is False
    assert sent == []
    alerts.report_run(status="partial", error_count=2, run_id=9, whatif=True)
    assert "PARTIAL" in sent[0] and "2 item error(s)" in sent[0] and "(what-if)" in sent[0]
    sent.clear()
    alerts.report_run(status="success", notices=["Revenue pruning suppressed: not certified"])
    assert "suppressed" in sent[0]
    sent.clear()
    alerts.report_run(status="error", exception="sync crashed: boom")
    assert "FAILED" in sent[0] and "boom" in sent[0]


def test_completed_run_reports_to_alerts(monkeypatch):
    from unittest.mock import MagicMock

    from app.sync.orchestrator import empty_catalog, empty_existing

    captured = {}

    def spy(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(alerts, "report_run", spy)
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=12)
    catalog = empty_catalog()
    catalog["_integrity"] = {
        "certified": False,
        "failed_reads": 0,
        "detail_failures": 0,
        "contract_rows": 3,
        "truncated_sources": ["GET /Contract/Get"],
    }
    result = SyncOrchestrator().run(
        SyncContext(trigger="manual", whatif=True, entities=["revenue"]),
        repo=repo,
        catalog=catalog,
        existing=empty_existing(),
    )
    assert result["notices"]  # suppressed-prune notice exists
    assert captured.get("run_id") == 12
    assert captured.get("notices") == result["notices"]


# ---------------------------------------------------------------------------
# dead-letter cockpit
# ---------------------------------------------------------------------------
def _isolate(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'dlq.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr("app.db.repo.SessionLocal", sessionmaker(bind=engine))
    return engine


def _login_admin(client: TestClient):
    client.post("/ui/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)


def test_deadletters_page_lists_retries_and_deletes(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = Repo()
    repo.add_dead_letter("deal", "49", "HTTP 500 bad association", {"dealname": "x"}, attempts=0)
    dead_id = repo.list_dead_letters()[0].id

    client = TestClient(app)
    _login_admin(client)
    page = client.get("/ui/deadletters").text
    assert "HTTP 500 bad association" in page and "49" in page

    retry = client.post("/ui/deadletters", data={"row_id": str(dead_id), "action": "retry"}, follow_redirects=False)
    assert retry.status_code == 303
    from app.db.models import DeadLetter

    row = Repo().session.get(DeadLetter, dead_id)
    assert row is not None and row.attempts == 1
    queued = Repo().session.execute(select(func.count()).select_from(WorkQueue)).scalar()
    assert queued == 1  # targeted retry sync is in the queue (worker does not run in web role)

    client.post("/ui/deadletters", data={"row_id": str(dead_id), "action": "delete"}, follow_redirects=False)
    assert Repo().list_dead_letters() == []
    gone = client.get("/ui/deadletters").text
    assert "Nothing stuck" in gone


def test_deadletters_requires_admin(tmp_path, monkeypatch):
    from app.auth import hash_password

    _isolate(tmp_path, monkeypatch)
    Repo().upsert_user("rep@x.com", "Rep", "sales", hash_password("rep-password-1"))
    client = TestClient(app)
    client.post("/ui/login", data={"username": "rep@x.com", "password": "rep-password-1"}, follow_redirects=False)
    assert client.get("/ui/deadletters").status_code == 403
