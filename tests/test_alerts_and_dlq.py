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
    from datetime import datetime

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
    assert row is not None and row.status == "open"
    # "Retry now" moves the row to the head of the reconciliation queue — the
    # scheduled job does the retry as a fresh targeted sync, so the web role
    # never enqueues a sync itself.
    assert [int(r.id) for r in Repo().due_dead_letters(datetime.utcnow())] == [dead_id]
    queued = Repo().session.execute(select(func.count()).select_from(WorkQueue)).scalar()
    assert queued == 0

    resolved = client.post(
        "/ui/deadletters", data={"row_id": str(dead_id), "action": "resolved"}, follow_redirects=False
    )
    assert resolved.status_code == 303
    row = Repo().session.get(DeadLetter, dead_id)
    assert row.status == "resolved" and "marked resolved" in row.resolution
    assert "Nothing pending" in client.get("/ui/deadletters").text

    client.post("/ui/deadletters", data={"row_id": str(dead_id), "action": "delete"}, follow_redirects=False)
    assert Repo().list_dead_letters() == []


def test_deadletters_requires_admin(tmp_path, monkeypatch):
    from app.auth import hash_password

    _isolate(tmp_path, monkeypatch)
    Repo().upsert_user("rep@x.com", "Rep", "sales", hash_password("rep-password-1"))
    client = TestClient(app)
    client.post("/ui/login", data={"username": "rep@x.com", "password": "rep-password-1"}, follow_redirects=False)
    assert client.get("/ui/deadletters").status_code == 403


# ---------------------------------------------------------------------------
# reconciliation spine: dedup, backoff, freeze, auto-resolve
# ---------------------------------------------------------------------------
def _dlq_settings(monkeypatch):
    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "dlq_retry_minutes", 10, raising=False)
    monkeypatch.setattr(settings, "dlq_freeze_after", 2, raising=False)
    return settings


def test_repeat_failures_dedup_without_double_charging_the_budget(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)  # freeze_after=2
    repo = Repo()
    repo.add_dead_letter("deal", "55", "stage rejected", {"dealname": "x"}, attempts=1)
    repo.add_dead_letter("deal", "55", "stage rejected again", {"dealname": "x"}, attempts=0)
    repo.add_dead_letter("deal", "55", "stage rejected once more", {"dealname": "x"}, attempts=0)

    rows = Repo().list_dead_letters(("open", "frozen"))
    assert len(rows) == 1  # the queue stays a queue: one row per record
    assert rows[0].attempts == 1  # failures never charge the cycle budget — only mark_reconciled does
    assert rows[0].error == "stage rejected once more"
    # ...so failure logging alone can never freeze a row: freeze lives on the
    # one path that also raises the Teams alert.
    assert rows[0].status == "open"


def test_reconcile_retries_as_fresh_targeted_syncs_then_freezes(tmp_path, monkeypatch):
    from datetime import datetime

    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)  # retry 10 min, freeze_after 2
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "whatif", False, raising=False)
    repo = Repo()
    repo.add_dead_letter("deal", "55", "stage rejected", {"dealname": "x"}, attempts=0, hubspot_id="d-9")
    for row in repo.list_dead_letters(("open",)):
        repo.retry_dead_letter_now(row.id)  # move to the front of the queue

    import app.sync.worker as worker_mod

    queued: list = []
    monkeypatch.setattr(worker_mod, "enqueue_sync", lambda ctx: queued.append(ctx))
    import app.alerts as alerts_mod

    sent: list = []
    monkeypatch.setattr(alerts_mod, "notify_teams", lambda text: sent.append(text) or True)

    from app.jobs import reconcile

    result = reconcile.run_reconciliation()
    assert result["retried"] == 1 and result["frozen"] == 0
    ctx = queued[0]
    assert tuple(ctx.entities) == ("deals",) and ctx.aquira_id == "55"
    assert ctx.whatif is False and ctx.trigger == "reconcile"
    row = Repo().list_dead_letters(("open",))[0]
    assert row.attempts == 1  # exactly one charge for one cycle
    assert 9 * 60 < (row.next_retry_at - datetime.utcnow()).total_seconds() < 11 * 60
    assert Repo().due_dead_letters(datetime.utcnow()) == []  # inside the backoff window

    # The retried sync fails and re-logs the record: the row updates but the
    # budget is NOT charged twice (enqueue + failure = one cycle, one charge).
    Repo().add_dead_letter("deal", "55", "stage rejected v2", {"dealname": "x"}, attempts=0, hubspot_id="d-9")
    assert Repo().list_dead_letters(("open",))[0].attempts == 1

    Repo().retry_dead_letter_now(row.id)
    result = reconcile.run_reconciliation()
    assert result["frozen"] == 1
    assert len(sent) == 1 and "/ui/deadletters" in sent[0] and "1 failed record write(s)" in sent[0]
    assert Repo().due_dead_letters(datetime(2999, 1, 1)) == []  # frozen: no API calls

    # "Fixed a setting? Retry all" restarts the budget.
    assert Repo().unfreeze_dead_letters() == 1
    assert len(Repo().due_dead_letters(datetime.utcnow())) == 1


def test_reconcile_backoff_grows_with_attempts(tmp_path, monkeypatch):
    from datetime import datetime

    _isolate(tmp_path, monkeypatch)
    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "dlq_retry_minutes", 10, raising=False)
    monkeypatch.setattr(settings, "dlq_freeze_after", 4, raising=False)
    monkeypatch.setattr(settings, "whatif", False, raising=False)
    repo = Repo()
    repo.add_dead_letter("deal", "55", "boom", {"dealname": "x"}, attempts=0)
    repo.retry_dead_letter_now(repo.list_dead_letters(("open",))[0].id)
    monkeypatch.setattr("app.sync.worker.enqueue_sync", lambda ctx: None)

    from app.jobs import reconcile

    reconcile.run_reconciliation()
    row = Repo().list_dead_letters(("open",))[0]
    assert 9 * 60 < (row.next_retry_at - datetime.utcnow()).total_seconds() < 11 * 60  # 10 x 1
    Repo().retry_dead_letter_now(row.id)
    reconcile.run_reconciliation()
    row = Repo().list_dead_letters(("open",))[0]
    assert 19 * 60 < (row.next_retry_at - datetime.utcnow()).total_seconds() < 21 * 60  # 10 x 2


def test_reconcile_defers_entirely_in_plan_only_mode(tmp_path, monkeypatch):
    from datetime import datetime

    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)
    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "whatif", True, raising=False)
    repo = Repo()
    repo.add_dead_letter("deal", "55", "stage rejected", {"dealname": "x"}, attempts=0)
    repo.retry_dead_letter_now(repo.list_dead_letters(("open",))[0].id)
    enq = []
    monkeypatch.setattr("app.sync.worker.enqueue_sync", lambda ctx: enq.append(ctx))

    from app.jobs import reconcile

    assert reconcile.run_reconciliation().get("skipped") == "plan-only mode"
    assert enq == []  # plan-only mode is the write-stop switch; this job must honor it
    row = Repo().list_dead_letters(("open",))[0]
    assert row.attempts == 0 and row.status == "open"  # and not burn the budget while deferring
    assert [r.id for r in Repo().due_dead_letters(datetime.utcnow())] == [row.id]


def test_reconcile_skips_when_worker_busy(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "whatif", False, raising=False)
    Repo().add_dead_letter("deal", "55", "boom", {"dealname": "x"}, attempts=0)
    enq = []
    monkeypatch.setattr("app.sync.worker.enqueue_sync", lambda ctx: enq.append(ctx))
    monkeypatch.setattr("app.sync.worker.is_busy", lambda: True)

    from app.jobs import reconcile

    assert reconcile.run_reconciliation().get("skipped") == "worker busy"
    assert enq == []  # the storm guard: never stack 50 retries behind a running sync


def test_reconcile_holds_client_creates_and_writeback_off_rows_without_burning_budget(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)
    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "whatif", False, raising=False)
    monkeypatch.setattr(settings, "sync_writeback", False, raising=False)
    repo = Repo()
    repo.add_dead_letter("client", None, "create blew up", {"Name": "X"}, attempts=0, hubspot_id="hs-9")
    repo.add_dead_letter("client", "44", "update failed", {"Name": "X"}, attempts=0, hubspot_id="hs-44")
    for row in repo.list_dead_letters(("open",)):
        repo.retry_dead_letter_now(row.id)
    enq = []
    monkeypatch.setattr("app.sync.worker.enqueue_sync", lambda ctx: enq.append(ctx))
    sent = []
    monkeypatch.setattr("app.alerts.notify_teams", lambda text: sent.append(text) or True)

    from app.jobs import reconcile

    result = reconcile.run_reconciliation()
    # Neither row CAN be retried automatically today: no sync is queued, no
    # cycle is charged — they are held with the reason and one alert instead.
    assert result["retried"] == 0 and result["held"] == 2 and enq == []
    rows = {r.aquira_id or "create": r for r in Repo().list_dead_letters(("frozen",))}
    assert rows["create"].attempts == 0 and "create gate" in rows["create"].resolution.lower()
    assert rows["44"].attempts == 0 and "sync_writeback" in rows["44"].resolution
    assert len(sent) == 1 and "+0 more" not in sent[0]


def test_reconcile_parks_rows_already_past_budget_without_a_final_sync(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)  # freeze_after=2
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "whatif", False, raising=False)
    repo = Repo()
    repo.add_dead_letter("deal", "55", "boom", {"dealname": "x"}, attempts=2)  # arrived over budget
    repo.retry_dead_letter_now(repo.list_dead_letters(("open",))[0].id)
    enq = []
    monkeypatch.setattr("app.sync.worker.enqueue_sync", lambda ctx: enq.append(ctx))
    monkeypatch.setattr("app.alerts.notify_teams", lambda text: True)

    from app.jobs import reconcile

    result = reconcile.run_reconciliation()
    assert result["retried"] == 0 and result["held"] == 1 and enq == []
    row = Repo().list_dead_letters(("open", "frozen"))[0]
    assert row.status == "frozen" and row.attempts == 2 and "exhausted" in row.resolution


def test_reconcile_maps_revenue_period_rows_to_their_owning_contract(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "whatif", False, raising=False)
    repo = Repo()
    # The row id is the synthetic period key, not any loadable Aquira id.
    repo.add_dead_letter("revenue_period", "9:2026-01:3", "assoc failed", {"x": 1}, attempts=0, hubspot_id="rp-1")
    repo.retry_dead_letter_now(repo.list_dead_letters(("open",))[0].id)
    enq = []
    monkeypatch.setattr("app.sync.worker.enqueue_sync", lambda ctx: enq.append(ctx))
    monkeypatch.setattr("app.alerts.notify_teams", lambda text: True)

    from app.jobs import reconcile

    reconcile.run_reconciliation()
    assert len(enq) == 1  # retried, not held
    assert tuple(enq[0].entities) == ("revenue",)
    assert enq[0].aquira_id == "9"  # the contract, not the period key


def test_reconcile_caps_per_pass_and_never_charges_a_failed_enqueue(tmp_path, monkeypatch):
    from datetime import datetime

    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)
    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "whatif", False, raising=False)
    monkeypatch.setattr(settings, "dlq_freeze_after", 50, raising=False)  # keep rows open while asserting
    repo = Repo()
    for i in range(12):
        repo.add_dead_letter("deal", str(100 + i), "boom", {"dealname": "x"}, attempts=0)
    for row in repo.list_dead_letters(("open",)):
        repo.retry_dead_letter_now(row.id)

    import app.sync.worker as worker_mod

    attempts_seen: list = []

    def flaky_enqueue(ctx):
        # the very first targeted sync blows up on the queue insert
        if not attempts_seen:
            attempts_seen.append(ctx)
            raise RuntimeError("queue unavailable")
        attempts_seen.append(ctx)

    monkeypatch.setattr(worker_mod, "enqueue_sync", flaky_enqueue)
    monkeypatch.setattr("app.alerts.notify_teams", lambda text: True)

    from app.jobs import reconcile

    result = reconcile.run_reconciliation()
    # Cap is 8 per pass; the group whose enqueue raised is not one of them.
    assert result["retried"] == 8 and len(attempts_seen) == 9
    rows = Repo().list_dead_letters(("open",))
    charged = [r for r in rows if (r.attempts or 0) >= 1]
    uncharged = [r for r in rows if (r.attempts or 0) == 0]
    assert len(charged) == 8 and len(uncharged) == 4  # 1 raised + 3 never reached (cap)
    # Rows without a charged cycle stay due: the cap defers, it never drops.
    assert len(Repo().due_dead_letters(datetime.utcnow())) == 4


def test_live_write_success_auto_resolves_the_row(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)
    repo = Repo()
    repo.add_dead_letter("client", None, "create failed", {"Name": "X"}, attempts=1, hubspot_id="hs-7")
    repo.add_dead_letter("deal", "55", "stage rejected", {"dealname": "y"}, attempts=1, hubspot_id="d-9")

    # The later sync created the client: the success item knows the new
    # Aquira id, the open row only ever knew the HubSpot one — the OR-match
    # across both keys is what closes it.
    closed = repo.resolve_dead_letters(
        [{"entity_type": "client", "aquira_id": "42", "hubspot_id": "hs-7"}], "written by sync #7"
    )
    assert closed == 1
    assert [r.entity_type for r in Repo().list_dead_letters(("open", "frozen"))] == ["deal"]
    resolved = Repo().list_dead_letters(("resolved",))[0]
    assert resolved.resolution == "written by sync #7" and resolved.resolved_at is not None

    # A fresh failure after resolution is a new open row, not a reopen.
    repo.add_dead_letter("client", "42", "update failed", {"Name": "X"}, attempts=0, hubspot_id="hs-7")
    open_rows = Repo().list_dead_letters(("open", "frozen"))
    assert len(open_rows) == 2 and {r.entity_type for r in open_rows} == {"client", "deal"}


def test_reconcile_refreshes_the_settings_overlay(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "whatif", False, raising=False)
    calls: list = []
    import app.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "apply_db_overlay", lambda: calls.append(1) or get_settings())

    from app.jobs import reconcile

    reconcile.run_reconciliation()
    assert calls  # the web container saved the fix; this process must re-read it, not boot values


def test_overlong_ids_are_clamped_instead_of_killing_the_row(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _dlq_settings(monkeypatch)
    Repo().add_dead_letter("client", None, "boom", {"x": 1}, attempts=0, hubspot_id="h" * 300)
    rows = Repo().list_dead_letters(("open",))
    assert len(rows) == 1 and len(rows[0].hubspot_id) == 100  # varchar(100) — Postgres would have raised and the caller would have swallowed the row


def test_skip_action_does_not_resolve_the_dead_letter_row(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod
    from app.db.models import DeadLetter
    from app.db.repo import Repo
    from app.sync.orchestrator import SyncContext, SyncOrchestrator, empty_existing

    engine = create_engine(f"sqlite:///{tmp_path / 'skipres.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr("app.db.repo.SessionLocal", sessionmaker(bind=engine))
    repo = Repo()
    repo.add_dead_letter("deal", "9001", "stage rejected", {"dealname": "x"}, attempts=0, hubspot_id="d-9")
    row_id = Repo().list_dead_letters(("open",))[0].id

    contract = {"ID": 9001, "ContractCD": "C-9001", "IsContract": True, "IsProposal": False,
                "Cancelled": False, "StartDate": "2026-01-01", "EndDate": "2026-01-31",
                "TotalValue": 1000, "lines": []}
    catalog = {"clients": [], "contacts": [], "contracts": [contract], "reps": []}

    monkeypatch.setattr(SyncOrchestrator, "apply_item",
                        lambda self, item, *a, **k: {**item, "action": "skip"})
    result = SyncOrchestrator().run(
        SyncContext(trigger="test", whatif=False, entities=["deals"]),
        repo=repo, aquira=MagicMock(), hubspot=MagicMock(), catalog=catalog, existing=empty_existing(),
    )
    assert result["status"] in {"success", "partial"}
    row = Repo().session.get(DeadLetter, row_id)
    assert row.status == "open" and row.resolution is None  # a skip wrote NOTHING — the row stays on duty

    # Contrast: an actual update closes the row automatically.
    monkeypatch.setattr(SyncOrchestrator, "apply_item",
                        lambda self, item, *a, **k: {**item, "action": "update", "hubspotId": "d-9"})
    SyncOrchestrator().run(
        SyncContext(trigger="test", whatif=False, entities=["deals"]),
        repo=Repo(), aquira=MagicMock(), hubspot=MagicMock(), catalog=catalog, existing=empty_existing(),
    )
    closed = Repo().session.get(DeadLetter, row_id)
    assert closed.status == "resolved" and "written by sync" in (closed.resolution or "")


def test_schedule_reconciliation_registers_a_tz_aware_first_run(monkeypatch):
    from datetime import datetime, timezone

    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "dlq_retry_minutes", 1, raising=False)
    jobs: list = []

    class FakeScheduler:
        def add_job(self, func, trigger, **kw):
            jobs.append((func, trigger, kw))

    from app.jobs import reconcile

    reconcile.schedule_reconciliation(FakeScheduler())
    func, trigger, kw = jobs[0]
    assert func is reconcile.run_reconciliation and trigger == "interval"
    assert kw["minutes"] == 5  # clamped floor: DLQ_RETRY_MINUTES=1 must not hot-loop the API
    assert kw["id"] == "dead_letter_reconcile" and kw["replace_existing"] is True
    first_run = kw["next_run_time"]
    assert first_run.tzinfo is not None  # naive UTC read as scheduler-local fired ~5 h late
    delta = (first_run - datetime.now(timezone.utc)).total_seconds()
    assert 0 < delta < 300
