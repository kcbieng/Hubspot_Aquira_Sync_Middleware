from datetime import datetime

from fastapi.testclient import TestClient

from app.db.models import OwnerMap
from app.db.repo import Repo
from app.main import app
from app.mapping.owners import suggest_owner_map
from app.settings import get_settings
from app.sync.orchestrator import SyncContext, SyncOrchestrator
from app.sync.planner import company_properties, deal_properties, plan_companies, plan_deals
from tests.test_orchestrator_apply import FakeAquira, FakeHubSpot


def test_plan_deals_skips_when_hubspot_returns_stringified_values():
    contract = {
        "ID": 9001,
        "ContractCD": "C-9001",
        "Name": "Spring",
        "IsProposal": False,
        "IsContract": True,
        "Cancelled": False,
        "TotalValue": 3000,
        "StartDate": "2026-01-01",
        "EndDate": "2026-03-31",
        "AccountID": 101,
        "AdvertiserID": 101,
        "SalesRepID": None,
        "Status": "Booked",
        "SignDate": "",
        "Stations": "KCBI",
        "lines": [],
    }
    props = deal_properties(contract, "Sewell Cadillac")
    stringified = {}
    for key, value in props.items():
        if isinstance(value, bool):
            stringified[key] = "true" if value else "false"
        else:
            stringified[key] = str(value)
    existing = {"9001": {"hubspotId": "deal-1", "properties": stringified, "hash": "stale"}}
    items = plan_deals([contract], existing, {}, {"101": "Sewell Cadillac"})
    assert items[0]["action"] == "skip"
    assert items[0]["diffs"] == []


def test_plan_companies_accounts_before_child_advertisers():
    clients = [
        {
            "ID": 202,
            "Name": "Advertiser Co",
            "Phone": "2145550002",
            "Website": "",
            "PhysicalAddress": "",
            "City": "Dallas",
            "State": "TX",
            "IsAccount": False,
            "IsAdvertiser": True,
            "AccountID": 101,
        },
        {
            "ID": 101,
            "Name": "Account Co",
            "Phone": "2145550001",
            "Website": "",
            "PhysicalAddress": "",
            "City": "Dallas",
            "State": "TX",
            "IsAccount": True,
            "IsAdvertiser": False,
        },
    ]
    items = plan_companies(clients, {})
    assert [item["aquiraId"] for item in items] == ["101", "202"]
    assert items[1]["associations"]["parentCompanyId"] == "101"


def test_plan_companies_skips_when_extra_hubspot_keys_present():
    client = {
        "ID": 101,
        "Name": "Sewell Cadillac",
        "Phone": "2145550000",
        "Website": "sewell.com",
        "PhysicalAddress": "Dallas",
        "City": "Dallas",
        "State": "TX",
        "IsAccount": True,
        "IsAdvertiser": True,
    }
    props = company_properties(client)
    existing_props = {**props, "hs_object_id": "cmp-1", "createdate": "2026-01-01"}
    existing = {"101": {"hubspotId": "cmp-1", "properties": existing_props, "hash": "stale"}}
    items = plan_companies([client], existing)
    assert items[0]["action"] == "skip"


def test_owner_suggest_unique_last_name_nickname():
    suggestions = suggest_owner_map(
        [{"id": 9, "name": "James Whitaker", "email": ""}],
        [{"owner_id": "hs-jim", "name": "Jim Whitaker", "email": "jim@kcbi.org"}],
    )
    assert suggestions[0]["hubspot_owner_id"] == "hs-jim"
    assert suggestions[0]["enabled"] is True


def test_owner_map_does_not_clobber_operator_assignment(monkeypatch):
    from app.api.routes import owner_map

    class FakeAquira:
        def __init__(self, *args, **kwargs):
            pass

        def load_sales_reps(self):
            return [{"id": 1, "name": "Jane Smith", "email": "jane@acme.example"}]

        def close(self):
            return None

    class FakeHubSpotClient:
        def get_owners(self):
            return {"results": [{"ownerId": "hs-1", "email": "jane@acme.example", "name": "Jane Smith"}]}

        def list_sales_users(self):
            return [
                {
                    "owner_id": "hs-1",
                    "name": "Jane Smith",
                    "email": "jane@acme.example",
                    "kind": "sales",
                    "role": "Sales",
                    "super_admin": False,
                }
            ]

    repo = Repo()
    try:
        repo.session.merge(
            OwnerMap(
                aquira_user_id="1",
                aquira_name="Jane Smith",
                aquira_email="jane@acme.example",
                hubspot_owner_id="hs-operator",
                hubspot_name="Operator Pick",
                hubspot_email="other@example.com",
                enabled=True,
                suggested=False,
                updated_at=datetime.utcnow(),
            )
        )
        repo.session.commit()
        monkeypatch.setattr("app.api.routes.AquiraSessionClient", FakeAquira)
        monkeypatch.setattr("app.api.routes.HubSpotClient", FakeHubSpotClient)
        owner_map()
        saved = repo.session.get(OwnerMap, "1")
        assert saved is not None
        assert saved.hubspot_owner_id == "hs-operator"
        assert saved.suggested is False
    finally:
        repo.close()


def test_second_live_run_skips_unchanged_records():
    orchestrator = SyncOrchestrator()
    aquira = FakeAquira()
    hubspot = FakeHubSpot()
    catalog = aquira.load_catalog()
    empty = {"companies": [], "contacts": [], "deals": [], "revenue": [], "unsynced": []}
    first = orchestrator.run(
        SyncContext(trigger="test", whatif=False, entities=["companies", "contacts", "deals"]),
        aquira=aquira,
        hubspot=hubspot,
        catalog=catalog,
        existing=empty,
    )
    assert first["counts"].get("create", 0) >= 3

    existing = {"companies": [], "contacts": [], "deals": [], "revenue": [], "unsynced": []}
    for object_type, properties, ident in hubspot.upserts:
        group = "revenue" if object_type == "revenue_period" else object_type
        existing[group].append({"id": ident, "properties": properties})

    hubspot.upserts.clear()
    second = orchestrator.run(
        SyncContext(trigger="test", whatif=True, entities=["companies", "contacts", "deals"]),
        aquira=aquira,
        hubspot=hubspot,
        catalog=catalog,
        existing=existing,
    )
    assert second["counts"].get("skip", 0) >= 3
    assert second["counts"].get("create", 0) == 0
    assert hubspot.upserts == []


def test_production_api_requires_login():
    settings = get_settings()
    original = settings.environment
    settings.environment = "production"
    client = TestClient(app)
    try:
        blocked = client.post("/api/sync/run", json={"whatif": True, "entities": ["companies"]})
        assert blocked.status_code == 401
        login = client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert login.status_code == 200
        allowed = client.post("/api/sync/run", json={"whatif": True, "entities": ["companies"], "wait": True})
        assert allowed.status_code == 200
        assert allowed.json()["status"] in {"success", "partial", "error", "queued"}
    finally:
        settings.environment = original
