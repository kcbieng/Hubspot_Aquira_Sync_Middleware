"""Users + roles, persisted match suggestions, self-service link/dismiss, and
the SMTP digest — the loop from "we found a possible duplicate" to "a human
fixed it without calling IT"."""

from datetime import timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.db as db_mod
import app.notify as notify
from app.auth import hash_password, parse_user_session_token, user_session_token, verify_password
from app.db.models import MatchSuggestion, OwnerMap
from app.db.repo import Repo
from app.main import app
from app.mapping.matching import apply_match_rules
from app.sync.orchestrator import SyncOrchestrator


def _repo(tmp_path) -> Repo:
    engine = create_engine(f"sqlite:///{tmp_path / 'users.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    return Repo(session=sessionmaker(bind=engine)())


def _isolate_app_db(tmp_path, monkeypatch):
    """Route handlers construct Repo() against app.db.SessionLocal — point the
    whole app at a temp database so UI tests never touch the developer's."""
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("app.db.repo.SessionLocal", factory)
    return factory


# ---------------------------------------------------------------------------
# auth primitives
# ---------------------------------------------------------------------------
def test_password_hash_roundtrip():
    stored = hash_password("correct horse battery")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "garbage")


def test_session_token_is_signed_and_tamper_evident():
    token = user_session_token("rep@x.com", "sales")
    assert parse_user_session_token(token) == ("rep@x.com", "sales")
    assert parse_user_session_token(token.replace("sales", "admin")) is None
    assert parse_user_session_token("nonsense") is None


# ---------------------------------------------------------------------------
# suggestion persistence + assignment
# ---------------------------------------------------------------------------
def _notice(kind="company", aid="5", hid="hs-5"):
    return {
        "entityType": "match-suggestion",
        "suggestionEntity": kind,
        "aquiraId": aid,
        "hubspotId": hid,
        "action": "notice",
        "match": {"aquiraId": aid, "hubspotId": hid, "clientName": "Acme", "companyName": "ACME Inc",
                  "method": "similar-name", "reason": "below-auto-threshold", "score": 60},
        "name": "x", "diffs": [], "properties": {},
    }


def test_record_suggestions_assigns_by_owner_map(tmp_path):
    repo = _repo(tmp_path)
    repo.session.add(OwnerMap(aquira_user_id="7", aquira_sales_rep_id="7", hubspot_email="rep@x.com", enabled=True))
    repo.session.commit()
    catalog = {"clients": [{"ID": 5, "Name": "Acme", "SalesRepID": "7"}], "contacts": [], "contracts": [], "reps": []}
    SyncOrchestrator._record_suggestions(repo, [_notice()], catalog, {"companies": [], "contacts": []}, None)
    rows = repo.list_suggestions()
    assert len(rows) == 1 and rows[0].assignee_email == "rep@x.com" and rows[0].status == "pending"


def test_record_resolves_already_linked_and_never_resurrects_decided_pairs(tmp_path):
    repo = _repo(tmp_path)
    catalog = {"clients": [{"ID": 5}], "contacts": [], "contracts": [], "reps": []}
    SyncOrchestrator._record_suggestions(repo, [_notice()], catalog, {"companies": [], "contacts": []}, None)
    repo.add_match_exclusion("company", "5", "hs-5", "rep@x.com")
    repo.set_suggestion_status("company", "5", "hs-5", "dismissed")
    SyncOrchestrator._record_suggestions(repo, [_notice()], catalog, {"companies": [], "contacts": []}, None)
    assert repo.list_suggestions() == []  # dismissed stays out of the pending queue
    assert repo.session.get(MatchSuggestion, ("company", "5", "hs-5")).status == "dismissed"

    other = _notice(aid="9", hid="hs-9")
    catalog9 = {"clients": [{"ID": 9}], "contacts": [], "contracts": [], "reps": []}
    SyncOrchestrator._record_suggestions(repo, [other], catalog9, {"companies": [], "contacts": []}, None)
    existing = {"companies": [{"properties": {"aquira_id": "9"}}], "contacts": []}
    SyncOrchestrator._record_suggestions(repo, [], catalog9, existing, None)
    assert repo.session.get(MatchSuggestion, ("company", "9", "hs-9")).status == "linked"


def test_exclusions_hide_pairs_from_the_engine():
    rules = [{"id": 1, "name": "phone", "on_match": "link",
              "conditions": [{"aquira_field": "Phone", "hubspot_field": "phone", "mode": "phone"}]}]
    items = [{"ID": "5", "Phone": "2145550100"}]
    rows = [{"id": "hs-5", "properties": {"phone": "2145550100"}}]
    assert apply_match_rules(items, rows, rules)[0] == {"5": ("hs-5", "phone")}
    assert apply_match_rules(items, rows, rules, {("5", "hs-5")})[0] == {}


# ---------------------------------------------------------------------------
# UI: login, gating, link & dismiss
# ---------------------------------------------------------------------------
def test_role_login_and_gating(tmp_path, monkeypatch):
    _isolate_app_db(tmp_path, monkeypatch)
    repo = Repo()
    repo.upsert_user("rep@x.com", "Rep Person", "sales", hash_password("hunter-two-23"))

    client = TestClient(app)
    login = client.post("/ui/login", data={"username": "rep@x.com", "password": "hunter-two-23"}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/ui/matches"
    assert client.get("/ui/settings").status_code == 403
    assert client.get("/ui/matching").status_code == 403
    assert client.get("/ui/users").status_code == 403
    assert client.get("/ui/matches").status_code == 200
    bad = client.post("/ui/login", data={"username": "rep@x.com", "password": "wrong"}, follow_redirects=False)
    assert "Invalid credentials" in bad.text


def test_legacy_admin_still_works_alongside_users(tmp_path, monkeypatch):
    _isolate_app_db(tmp_path, monkeypatch)
    client = TestClient(app)
    login = client.post("/ui/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/ui"
    assert client.get("/ui/settings").status_code == 200
    assert client.get("/ui/users").status_code == 200


def test_link_and_dismiss_actions(tmp_path, monkeypatch):
    _isolate_app_db(tmp_path, monkeypatch)
    repo = Repo()
    repo.record_match_suggestion("company", "5", "hs-5", aquira_name="Acme", hubspot_name="ACME Inc",
                                 method="similar-name", reason="below-auto-threshold", score=60,
                                 assignee_email=None, run_id=None)

    patched = SimpleNamespace(calls=[])
    import app.hubspot.client as hubspot_client_module

    class FakeClient:
        def upsert_crm(self, obj, properties, existing_id):
            patched.calls.append((obj, dict(properties), existing_id))
            return {"id": existing_id}

    monkeypatch.setattr(hubspot_client_module, "HubSpotClient", FakeClient)
    client = TestClient(app)
    client.post("/ui/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)

    link = client.post("/ui/matches",
                       data={"action": "link", "entity_type": "company", "aquira_id": "5", "hubspot_id": "hs-5"},
                       follow_redirects=False)
    assert link.status_code == 303
    assert patched.calls == [("companies", {"aquira_id": "5"}, "hs-5")]
    assert Repo().list_suggestions() == []  # linked rows leave the pending queue

    Repo().record_match_suggestion("company", "6", "hs-6", aquira_name="Zeta", hubspot_name="Z", method="phone",
                                   reason="x", score=55, assignee_email=None, run_id=None)
    dismiss = client.post("/ui/matches",
                          data={"action": "dismiss", "entity_type": "company", "aquira_id": "6", "hubspot_id": "hs-6"},
                          follow_redirects=False)
    assert dismiss.status_code == 303
    assert Repo().exclusions_for("company") == {("6", "hs-6")}
    page = client.get("/ui/matches").text
    assert "Acme" not in page  # dismissed/linked pairs leave the queue...
    assert "Zeta" not in page
    assert "hs-6" in page      # ...while admins can still see and re-open exclusions

    reopen = client.post("/ui/matches",
                         data={"action": "remove-exclusion", "entity_type": "company", "aquira_id": "6", "hubspot_id": "hs-6"},
                         follow_redirects=False)
    assert reopen.status_code == 303
    assert Repo().exclusions_for("company") == set()


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------
def test_match_digest_groups_notifies_and_respects_window(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    repo.upsert_user("boss@x.com", "Boss", "admin", hash_password("x" * 10))
    for i, assignee in enumerate(["rep@x.com", "rep@x.com", None]):
        repo.record_match_suggestion("company", str(i), f"hs-{i}", aquira_name=f"Aq {i}", hubspot_name=f"Hs {i}",
                                     method="rule", reason="x", score=0, assignee_email=assignee, run_id=None)

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(notify, "send_email", lambda to, subject, body: sent.append((to, subject, body)) or True)
    monkeypatch.setattr(
        notify, "get_settings",
        lambda: SimpleNamespace(match_digest_enabled=True, smtp_host="smtp.test", smtp_port=587,
                                smtp_user="", smtp_password="", smtp_from="hubquira@x.com",
                                public_base_url="https://hq.example"),
    )

    result = notify.run_match_digest(repo_factory=lambda: repo)
    assert result["sent"] == 2
    assert {to for to, _, _ in sent} == {"rep@x.com", "boss@x.com"}  # unassigned falls to the first admin
    body = next(b for to, _s, b in sent if to == "rep@x.com")
    assert "2 potential duplicates" in body and "https://hq.example/ui/matches" in body

    again = notify.run_match_digest(repo_factory=lambda: repo)
    assert again["sent"] == 0  # inside the 20h notification window
    rows = repo.session.query(MatchSuggestion).all()
    assert all(r.last_notified_at is not None for r in rows)

    # stale rows become due again after the window
    backdated = repo.session.query(MatchSuggestion).all()
    for row in backdated:
        row.last_notified_at = row.last_notified_at - timedelta(hours=21)
    repo.session.commit()
    sent.clear()
    assert notify.run_match_digest(repo_factory=lambda: repo)["sent"] == 2


def test_digest_disabled_without_smtp(monkeypatch):
    monkeypatch.setattr(
        notify, "get_settings",
        lambda: SimpleNamespace(match_digest_enabled=True, smtp_host="", public_base_url=""),
    )
    assert notify.run_match_digest()["status"] == "skipped"
