"""Approval-gated Aquira client creation: the HubSpot 'Create in Aquira as…'
dropdown is the human gesture; blank means never; failures wait for a human."""

from unittest.mock import MagicMock

from app.aquira.client import AquiraSessionClient
from app.sync.orchestrator import SyncContext, SyncOrchestrator, empty_catalog, empty_existing
from app.sync.planner import plan_new_aquira_clients


def _company(hid="hs-1", name="New Media Co", create_as=None, aquira_id=None):
    props = {"name": name}
    if create_as:
        props["aquira_create_as"] = create_as
    return {"id": hid, "hubspotId": hid, "properties": props, "aquira_id": aquira_id}


def test_blank_dropdown_means_no_creation():
    assert plan_new_aquira_clients([_company()]) == []
    assert plan_new_aquira_clients([_company(create_as="garbage")]) == []
    assert plan_new_aquira_clients([_company(create_as="account", aquira_id="9")]) == []  # already linked


def test_selected_party_type_flows_into_the_create_plan():
    items = plan_new_aquira_clients(
        [
            _company(hid="hs-1", create_as="account"),
            _company(hid="hs-2", name="Brand Co", create_as="advertiser"),
            _company(hid="hs-3", name="Agency Brand", create_as="both"),
        ]
    )
    assert [(i["hubspotId"], i["createAs"]) for i in items] == [
        ("hs-1", "account"),
        ("hs-2", "advertiser"),
        ("hs-3", "both"),
    ]
    assert all(i["action"] == "create" and i["writeback"] for i in items)


def test_blocked_companies_wait_for_a_human():
    items = plan_new_aquira_clients([_company(create_as="account")], blocked_hubspot_ids={"hs-1"})
    assert items == []


def test_create_client_puts_party_type_on_the_entity():
    client = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")
    seen = []

    def fake_request(method, path, **kwargs):
        seen.append((method, path, kwargs.get("json")))
        if path == "/Client/Create":
            return {"Success": True, "Entity": {"ID": 42}}
        return {"Success": True, "Entity": {"ID": 42, "Name": "New Media Co"}}

    client.request = fake_request
    for party, (account, advertiser) in {
        "account": (True, False),
        "advertiser": (False, True),
        "both": (True, True),
    }.items():
        seen.clear()
        client.create_client({"Name": "New Media Co"}, party_type=party)
        entity = next(call for call in seen if call[1] == "/Client/Create")[2]["Entity"]
        assert (entity["IsAccount"], entity["IsAdvertiser"]) == (account, advertiser), party
    seen.clear()
    client.create_client({"Name": "X"})  # default = account; never a flag-less create
    entity = next(call for call in seen if call[1] == "/Client/Create")[2]["Entity"]
    assert (entity["IsAccount"], entity["IsAdvertiser"]) == (True, False)


def test_apply_passes_party_and_links_the_new_id_back():
    aquira = MagicMock()
    aquira.create_client.return_value = {"ID": 501}
    hubspot = MagicMock()
    hubspot.upsert_crm.return_value = {"id": "hs-1"}
    item = {
        "entityType": "client", "aquiraId": None, "hubspotId": "hs-1", "action": "create",
        "writeback": True, "createAs": "advertiser", "properties": {"Name": "Brand Co"},
    }
    applied = SyncOrchestrator().apply_item(item, aquira, hubspot, {})
    aquira.create_client.assert_called_once_with({"Name": "Brand Co"}, party_type="advertiser")
    assert applied["aquiraId"] == "501"
    hubspot.upsert_crm.assert_called_once_with("companies", {"aquira_id": "501"}, "hs-1")


def test_repo_surfaces_open_create_failures(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod
    from app.db.repo import Repo

    engine = create_engine(f"sqlite:///{tmp_path / 'gate.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    repo = Repo(session=sessionmaker(bind=engine)())
    repo.add_dead_letter("client", None, "create blew up", {"Name": "X", "_hubspotId": "hs-9"}, attempts=1)
    # An unconfirmed create blocks IMMEDIATELY (open counts): the create can
    # have committed in Aquira before raising, and re-approving it would
    # duplicate a master-system record that cannot be cleanly undone.
    assert repo.open_client_create_failures() == {"hs-9"}
    repo.add_dead_letter("client", None, "create blew up again", {"_hubspotId": "hs-9"}, attempts=0)
    assert len(repo.list_dead_letters(("open", "frozen"))) == 1  # folded, not duplicated
    assert repo.open_client_create_failures() == {"hs-9"}  # freezing is not what gates
    repo.add_dead_letter("deal", None, "deal failure", {"x": 1}, attempts=1)
    assert repo.open_client_create_failures() == {"hs-9"}  # client gate ignores other entities
    # The operator fixes the cause and resolves the row: the next writeback
    # pass re-proposes the create normally.
    create_row = next(r for r in repo.list_dead_letters(("open",)) if r.entity_type == "client")
    repo.resolve_dead_letter_by_id(create_row.id, "unblocked by hand")
    assert repo.open_client_create_failures() == set()


def test_fold_that_learns_the_aquira_id_releases_the_gate(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod
    from app.db.repo import Repo

    engine = create_engine(f"sqlite:///{tmp_path / 'gate2.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    repo = Repo(session=sessionmaker(bind=engine)())
    repo.add_dead_letter("client", None, "create failed reload", {"_hubspotId": "hs-9"}, attempts=1)
    assert repo.open_client_create_failures() == {"hs-9"}
    # A later failure arrives KNOWING the Aquira id (44): the create did land,
    # so the fold backfills the id and the gate releases — a retry now links,
    # it cannot duplicate.
    repo.add_dead_letter("client", "44", "update failed", {"_hubspotId": "hs-9"}, attempts=1)
    assert len(repo.list_dead_letters(("open", "frozen"))) == 1
    assert repo.open_client_create_failures() == set()


def test_plan_integration_respects_the_gate(monkeypatch):
    from app.settings import get_settings

    monkeypatch.setattr("app.sync.orchestrator.get_settings", lambda: type(get_settings())(sync_writeback=True))
    orchestrator = SyncOrchestrator()
    existing = empty_existing()
    existing["unsynced"] = [_company(create_as="account"), _company(hid="hs-2", name="Quiet Co")]
    items = orchestrator.build_plan(
        empty_catalog(), existing, ["writeback"], {}, create_missing_clients=True, create_blocked_ids={"hs-1"}
    )
    creates = [i for i in items if i.get("entityType") == "client" and i.get("action") == "create"]
    assert creates == []  # hs-1 is dead-letter-blocked; hs-2 was never approved

    items = orchestrator.build_plan(empty_catalog(), existing, ["writeback"], {}, create_missing_clients=True)
    creates = [i for i in items if i.get("entityType") == "client" and i.get("action") == "create"]
    assert [i["hubspotId"] for i in creates] == ["hs-1"]  # approval unlocks it
    assert creates[0]["createAs"] == "account"
