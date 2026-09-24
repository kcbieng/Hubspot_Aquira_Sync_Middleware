"""Custom-pipeline stage mapping: planner substitution, run() wiring from
settings, and the admin page."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings
from app.sync.orchestrator import SyncOrchestrator, empty_existing
from app.sync.planner import deal_properties, plan_deals


def _contract(**over):
    row = {"ID": 9, "ContractCD": "C9", "IsContract": True, "StartDate": "2026-01-01",
           "EndDate": "2026-01-31", "TotalValue": 1000, "lines": []}
    row.update(over)
    return row


MAP = {"pipeline": "pipe-guid-77", "proposal": "stage-proposal-guid",
       "closedwon": "stage-won-guid", "closedlost": "stage-lost-guid"}


def test_mapped_stages_replace_the_semantic_defaults():
    booked = deal_properties(_contract(), None, MAP)
    assert booked["pipeline"] == "pipe-guid-77" and booked["dealstage"] == "stage-won-guid"

    cancelled = deal_properties(_contract(Cancelled=True), None, MAP)
    assert cancelled["dealstage"] == "stage-lost-guid"

    open_proposal = deal_properties(_contract(IsContract=False, IsProposal=True), None, MAP)
    assert open_proposal["dealstage"] == "stage-proposal-guid"


def test_partial_map_leaves_unmapped_tokens_semantic():
    partial = {"closedwon": "won-only"}
    booked = deal_properties(_contract(), None, partial)
    assert booked["dealstage"] == "won-only" and booked["pipeline"] == "default"
    proposal = deal_properties(_contract(IsContract=False, IsProposal=True), None, partial)
    assert proposal["dealstage"] == "proposal"  # unmapped token untouched


def test_no_map_is_100_percent_previous_behavior():
    booked = deal_properties(_contract())
    assert booked["pipeline"] == "default" and booked["dealstage"] == "closedwon"


def test_build_plan_threads_stage_map_into_deal_items():
    orchestrator = SyncOrchestrator()
    catalog = {"clients": [], "contacts": [], "contracts": [_contract()], "reps": []}
    items = orchestrator.build_plan(catalog, empty_existing(), ["deals"], {}, stage_map=MAP)
    deal = next(i for i in items if i["entityType"] == "deal")
    assert deal["properties"]["dealstage"] == "stage-won-guid"
    assert deal["properties"]["pipeline"] == "pipe-guid-77"


def test_apply_skips_the_legacy_proposal_lookup_once_mapped(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "hubspot_stage_proposal", "stage-proposal-guid", raising=False)
    hubspot = MagicMock()
    hubspot.upsert_crm.return_value = {"id": "d-1"}
    item = {"entityType": "deal", "action": "update", "hubspotId": "d-1", "aquiraId": "9",
            "properties": {"dealstage": "stage-proposal-guid", "pipeline": "pipe"}}
    SyncOrchestrator().apply_item(item, None, hubspot, {})
    hubspot.ensure_proposal_stage.assert_not_called()

    monkeypatch.setattr(settings, "hubspot_stage_proposal", "", raising=False)
    hubspot2 = MagicMock()
    hubspot2.upsert_crm.return_value = {"id": "d-1"}
    hubspot2.ensure_proposal_stage.return_value = "found-me"
    item2 = {"entityType": "deal", "action": "update", "hubspotId": "d-1", "aquiraId": "9",
             "properties": {"dealstage": "proposal"}}
    SyncOrchestrator().apply_item(item2, None, hubspot2, {})
    hubspot2.ensure_proposal_stage.assert_called_once()
    assert hubspot2.upsert_crm.call_args[0][1]["dealstage"] == "found-me"


def test_won_only_map_still_repairs_the_proposal_stage(monkeypatch):
    """The old all-or-nothing guard let a won-only mapping silence the legacy
    proposal rescue — every open proposal then 400'd with the raw token. The
    guard must key on the proposal mapping itself."""
    settings = get_settings()
    monkeypatch.setattr(settings, "hubspot_stage_proposal", "", raising=False)
    monkeypatch.setattr(settings, "hubspot_stage_won", "won-guid-1", raising=False)
    monkeypatch.setattr(settings, "hubspot_deal_pipeline", "", raising=False)
    hubspot = MagicMock()
    hubspot.upsert_crm.return_value = {"id": "d-1"}
    hubspot.ensure_proposal_stage.return_value = "repaired"
    item = {"entityType": "deal", "action": "update", "hubspotId": "d-1", "aquiraId": "9",
            "properties": {"dealstage": "proposal"}}
    SyncOrchestrator().apply_item(item, None, hubspot, {})
    hubspot.ensure_proposal_stage.assert_called_once()
    assert hubspot.upsert_crm.call_args[0][1]["dealstage"] == "repaired"


def test_custom_pipeline_map_does_not_trigger_the_legacy_lookup(monkeypatch):
    """ensure_proposal_stage picks from the account's FIRST pipeline — with a
    custom pipeline configured its answer is a wrong-pipeline id (a different
    400). An explicit custom pipeline must write the token and let the visible
    400 reach the dead-letter page, not a silently mis-targeted id."""
    settings = get_settings()
    monkeypatch.setattr(settings, "hubspot_stage_proposal", "", raising=False)
    monkeypatch.setattr(settings, "hubspot_deal_pipeline", "pipe-7", raising=False)
    hubspot = MagicMock()
    hubspot.upsert_crm.return_value = {"id": "d-1"}
    item = {"entityType": "deal", "action": "update", "hubspotId": "d-1", "aquiraId": "9",
            "properties": {"dealstage": "proposal", "pipeline": "pipe-7"}}
    SyncOrchestrator().apply_item(item, None, hubspot, {})
    hubspot.ensure_proposal_stage.assert_not_called()
    assert hubspot.upsert_crm.call_args[0][1]["dealstage"] == "proposal"


def test_deal_pipelines_transforms_the_api_payload():
    from app.hubspot.client import HubSpotClient

    client = HubSpotClient.__new__(HubSpotClient)  # no auth: only the transform is under test
    client._request = lambda method, path, **kw: {"results": [
        {"id": "p1", "label": "Media", "stages": [{"id": "g1", "label": "Won"}, {"id": "g2"}]},
        {"id": "p2", "stages": []},
    ]}
    pipelines = client.deal_pipelines()
    assert pipelines[0] == {"id": "p1", "label": "Media",
                            "stages": [{"id": "g1", "label": "Won"}, {"id": "g2", "label": "g2"}]}
    assert pipelines[1]["label"] == "p2"  # label falls back to the id, both stay strings


PIPELINES = [
    {"id": "pipe-a", "label": "Media Sales", "stages": [
        {"id": "s1", "label": "New Proposal"},
        {"id": "s2", "label": "Proposal Sent"},
        {"id": "s3", "label": "Won - Media"},
        {"id": "s4", "label": "Lost"},
    ]},
]


def test_stages_page_and_save(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod

    engine = create_engine(f"sqlite:///{tmp_path / 'stages.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr("app.db.repo.SessionLocal", sessionmaker(bind=engine))

    import app.hubspot.client as hubspot_client_module

    monkeypatch.setattr(hubspot_client_module, "HubSpotClient", lambda *a, **k: SimpleNamespace(deal_pipelines=lambda: PIPELINES))
    settings = get_settings()
    for name in ("hubspot_deal_pipeline", "hubspot_stage_proposal", "hubspot_stage_won", "hubspot_stage_lost"):
        monkeypatch.setattr(settings, name, getattr(settings, name), raising=False)

    client = TestClient(app)
    client.post("/ui/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    page = client.get("/ui/stages")
    assert page.status_code == 200 and "Media Sales" in page.text
    assert "default pipeline (built-in stages)" in page.text  # the blank choice must exist

    saved = client.post("/ui/stages", data={
        "action": "save", "pipeline": "pipe-a", "stage_proposal": "s2", "stage_won": "s3", "stage_lost": "s4",
    }, follow_redirects=False)
    assert saved.status_code == 303
    assert settings.hubspot_stage_won == "s3" and settings.hubspot_deal_pipeline == "pipe-a"
    from app.db.repo import Repo

    # The DB half is what survives a restart (and reaches the worker) — pin it.
    assert Repo().get_setting("hubspot_stage_won") == "s3"

    detected = client.post("/ui/stages", data={"action": "autodetect", "pipeline": "pipe-a"}, follow_redirects=False)
    assert detected.status_code == 303
    assert settings.hubspot_stage_proposal == "s1"  # first label containing "proposal"
    assert settings.hubspot_stage_won == "s3" and settings.hubspot_stage_lost == "s4"


def test_partial_stage_save_is_rejected_without_writing(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod

    engine = create_engine(f"sqlite:///{tmp_path / 'stages3.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr("app.db.repo.SessionLocal", sessionmaker(bind=engine))
    import app.hubspot.client as hubspot_client_module

    monkeypatch.setattr(hubspot_client_module, "HubSpotClient", lambda *a, **k: SimpleNamespace(deal_pipelines=lambda: PIPELINES))
    settings = get_settings()
    for name in ("hubspot_deal_pipeline", "hubspot_stage_proposal", "hubspot_stage_won", "hubspot_stage_lost"):
        monkeypatch.setattr(settings, name, getattr(settings, name), raising=False)
    client = TestClient(app)
    client.post("/ui/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)

    # Custom pipeline + a half-finished map is exactly the guaranteed-400 state.
    bad = client.post("/ui/stages", data={"action": "save", "pipeline": "pipe-a", "stage_won": "s3"}, follow_redirects=False)
    assert bad.status_code == 200 and "all three" in bad.text
    assert settings.hubspot_stage_won == "" and settings.hubspot_deal_pipeline == ""
    # A stage id from another pipeline never lands either.
    leaked = client.post("/ui/stages", data={
        "action": "save", "pipeline": "pipe-a", "stage_proposal": "s2",
        "stage_won": "s3", "stage_lost": "not-on-this-pipeline",
    }, follow_redirects=False)
    assert leaked.status_code == 200 and "not on pipeline" in leaked.text
    assert settings.hubspot_stage_proposal == ""


def test_autodetect_failure_saves_nothing_and_blank_tokens_cannot_wipe(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod

    engine = create_engine(f"sqlite:///{tmp_path / 'stages4.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr("app.db.repo.SessionLocal", sessionmaker(bind=engine))
    import app.hubspot.client as hubspot_client_module

    settings = get_settings()
    monkeypatch.setattr(settings, "hubspot_deal_pipeline", "pipe-a", raising=False)
    monkeypatch.setattr(settings, "hubspot_stage_proposal", "keep-proposal", raising=False)
    monkeypatch.setattr(settings, "hubspot_stage_won", "keep-won", raising=False)
    monkeypatch.setattr(settings, "hubspot_stage_lost", "keep-lost", raising=False)
    client = TestClient(app)
    client.post("/ui/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)

    # HubSpot transiently down: the old code persisted three blanks — wiping
    # the working mapping AND the only page with a form to fix it.
    def boom():
        raise RuntimeError("401 token expired")

    monkeypatch.setattr(hubspot_client_module, "HubSpotClient", lambda *a, **k: SimpleNamespace(deal_pipelines=boom))
    failed = client.post("/ui/stages", data={"action": "autodetect", "pipeline": "pipe-a"}, follow_redirects=False)
    assert failed.status_code == 200 and "nothing was saved" in failed.text
    assert settings.hubspot_stage_won == "keep-won" and settings.hubspot_deal_pipeline == "pipe-a"

    # Up again, but labels match only the proposal: unmatched tokens KEEP the
    # operator's ids instead of being blanked to the broken hybrid.
    odd = [{"id": "pipe-a", "label": "Media Sales", "stages": [
        {"id": "s9", "label": "Proposal Sent"}, {"id": "s8", "label": "Doing Things"},
    ]}]
    monkeypatch.setattr(hubspot_client_module, "HubSpotClient", lambda *a, **k: SimpleNamespace(deal_pipelines=lambda: odd))
    ok = client.post("/ui/stages", data={"action": "autodetect", "pipeline": "pipe-a"}, follow_redirects=False)
    assert ok.status_code == 303
    assert settings.hubspot_stage_proposal == "s9"  # improved
    assert settings.hubspot_stage_won == "keep-won" and settings.hubspot_stage_lost == "keep-lost"  # not wiped


def test_stages_page_requires_admin(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod
    from app.auth import hash_password
    from app.db.repo import Repo

    engine = create_engine(f"sqlite:///{tmp_path / 'stages2.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr("app.db.repo.SessionLocal", sessionmaker(bind=engine))
    Repo().upsert_user("rep@x.com", "Rep", "sales", hash_password("secret-pass-9"))
    client = TestClient(app)
    client.post("/ui/login", data={"username": "rep@x.com", "password": "secret-pass-9"}, follow_redirects=False)
    assert client.get("/ui/stages").status_code == 403
