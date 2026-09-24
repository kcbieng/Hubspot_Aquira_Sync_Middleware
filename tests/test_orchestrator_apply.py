from app.sync.orchestrator import SyncContext, SyncOrchestrator


class FakeAquira:
    def __init__(self):
        self.puts = []
        self.created = []
        self.logged_in = False
        self.version = "test"

    def login(self):
        self.logged_in = True
        return {"Success": True}

    def logout(self):
        self.logged_in = False

    def load_catalog(self, aquira_id=None):
        return {
            "clients": [
                {
                    "ID": 101,
                    "Name": "Sewell Cadillac",
                    "Phone": "2145550000",
                    "Website": "sewell.com",
                    "PhysicalAddress": "Dallas",
                    "City": "Dallas",
                    "State": "TX",
                    "IsAccount": True,
                    "IsAdvertiser": True,
                    "Contacts": [],
                }
            ],
            "contacts": [
                {"ID": 501, "ClientID": 101, "FirstName": "Carl", "LastName": "Sewell", "Email": "carl@sewell.com", "Phone": "2145550000"}
            ],
            "contracts": [
                {
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
                    "lines": [],
                }
            ],
            "reps": [],
        }

    def update_client_sparse(self, aquira_id, fields):
        self.puts.append((aquira_id, fields))
        return {"Success": True}

    def create_client(self, fields, party_type="account"):
        self.created.append((fields, party_type))
        return {"ID": 777, "Name": fields.get("Name")}


class FakeHubSpot:
    def __init__(self):
        self.upserts = []
        self.associations = []
        self.revenue_object_type = "revenue_period"

    def ensure_crm_schema(self):
        return {"created": [], "warnings": []}

    def ensure_proposal_stage(self):
        return "proposal"

    def projection(self):
        return {"companies": [], "contacts": [], "deals": [], "revenue": [], "owners": []}

    def upsert_crm(self, object_type, properties, existing_id=None):
        ident = existing_id or f"{object_type}-{len(self.upserts)+1}"
        self.upserts.append((object_type, properties, ident))
        return {"id": ident, "properties": properties}

    def associate(self, *args, **kwargs):
        self.associations.append(args)


def test_live_apply_writes_hubspot_records():
    orchestrator = SyncOrchestrator()
    aquira = FakeAquira()
    hubspot = FakeHubSpot()
    result = orchestrator.run(
        SyncContext(trigger="test", whatif=False, entities=["companies", "contacts", "deals"]),
        aquira=aquira,
        hubspot=hubspot,
        catalog=aquira.load_catalog(),
        existing={"companies": [], "contacts": [], "deals": [], "revenue": [], "unsynced": []},
    )
    assert result["status"] == "success"
    assert result["counts"].get("create", 0) >= 3
    object_types = [row[0] for row in hubspot.upserts]
    assert "companies" in object_types
    assert "contacts" in object_types
    assert "deals" in object_types
    assert "revenue_period" in object_types
    assert aquira.puts == []


def test_writeback_stays_off_unless_enabled(monkeypatch):
    from app.settings import get_settings

    monkeypatch.setattr("app.sync.orchestrator.get_settings", lambda: type(get_settings())(sync_writeback=False))
    orchestrator = SyncOrchestrator()
    wanted = orchestrator._wanted(["companies", "contacts", "writeback"])
    assert "writeback" not in wanted


def test_whatif_does_not_write():
    orchestrator = SyncOrchestrator()
    aquira = FakeAquira()
    hubspot = FakeHubSpot()
    orchestrator.run(
        SyncContext(trigger="test", whatif=True, entities=["companies"]),
        aquira=aquira,
        hubspot=hubspot,
        catalog=aquira.load_catalog(),
        existing={"companies": [], "contacts": [], "deals": [], "revenue": [], "unsynced": []},
    )
    assert hubspot.upserts == []
    assert aquira.puts == []


def test_skip_still_associates_parent_company():
    orchestrator = SyncOrchestrator()
    aquira = FakeAquira()
    hubspot = FakeHubSpot()
    catalog = {
        "clients": [
            {"ID": 101, "Name": "Agency", "IsAccount": True, "IsAdvertiser": False, "Attributes": {}},
            {"ID": 202, "Name": "Advertiser", "IsAccount": False, "IsAdvertiser": True, "AccountID": 101, "Attributes": {}},
        ],
        "contacts": [],
        "contracts": [],
        "reps": [],
    }
    existing = {
        "companies": [
            {"id": "hs-agency", "properties": {"aquira_id": "101", "name": "Agency"}},
            {"id": "hs-adv", "properties": {"aquira_id": "202", "name": "Advertiser"}},
        ],
        "contacts": [],
        "deals": [],
        "revenue": [],
        "unsynced": [],
    }
    orchestrator.run(
        SyncContext(trigger="test", whatif=False, entities=["companies"]),
        aquira=aquira,
        hubspot=hubspot,
        catalog=catalog,
        existing=existing,
    )
    pairs = {(row[1], row[3], row[4]) for row in hubspot.associations}
    assert ("hs-adv", "hs-agency", 14) in pairs
    assert ("hs-agency", "hs-adv", 13) in pairs


def test_partial_sync_does_not_archive_other_contracts_revenue():
    orchestrator = SyncOrchestrator()
    catalog = {
        "clients": [],
        "contacts": [],
        "contracts": [
            {
                "ID": 42,
                "ContractCD": "1064",
                "IsContract": True,
                "Cancelled": False,
                "TotalValue": 320,
                "StartDate": "2026-10-01",
                "EndDate": "2026-10-31",
                "AccountID": 3,
                "AdvertiserID": 3,
                "lines": [],
            }
        ],
        "reps": [],
    }
    existing = {
        "companies": [],
        "contacts": [],
        "deals": [{"id": "hs-deal-42", "properties": {"aquira_id": "42", "dealname": "1064", "amount": 320, "pipeline": "default", "dealstage": "closedwon"}}],
        "revenue": [
            {
                "id": "hs-rev-other",
                "properties": {
                    "aquira_id": "99:2026-10:0",
                    "deal_aquira_id": "99",
                    "period": "2026-10-01",
                    "amount": 500,
                    "spot_amount": 500,
                    "charge_amount": 0,
                    "source": "spot",
                    "station": "KCBI",
                    "station_id": 0,
                    "kind": "booked",
                    "contract_cd": "0999",
                },
            }
        ],
        "unsynced": [],
    }
    hubspot = FakeHubSpot()
    hubspot.archives = []
    hubspot.archive = lambda *args, **kwargs: hubspot.archives.append(args)
    result = orchestrator.run(
        SyncContext(trigger="webhook", whatif=False, entities=["deals"], aquira_id="42"),
        aquira=FakeAquira(),
        hubspot=hubspot,
        catalog=catalog,
        existing=existing,
    )
    assert result["counts"].get("delete-stale", 0) == 0
    assert hubspot.archives == []


def test_full_sync_does_not_archive_unloaded_contract_revenue():
    orchestrator = SyncOrchestrator()
    catalog = {
        "clients": [],
        "contacts": [],
        "contracts": [
            {
                "ID": 42,
                "ContractCD": "1064",
                "IsContract": True,
                "Cancelled": False,
                "TotalValue": 320,
                "StartDate": "2026-10-01",
                "EndDate": "2026-10-31",
                "AccountID": 3,
                "AdvertiserID": 3,
                "lines": [],
            }
        ],
        "reps": [],
    }
    existing = {
        "companies": [],
        "contacts": [],
        "deals": [],
        "revenue": [
            {
                "id": "hs-rev-other",
                "properties": {
                    "aquira_id": "99:2026-10:0",
                    "deal_aquira_id": "99",
                    "period": "2026-10-01",
                    "amount": 500,
                    "kind": "booked",
                    "contract_cd": "0999",
                },
            }
        ],
        "unsynced": [],
    }
    hubspot = FakeHubSpot()
    hubspot.archives = []
    hubspot.archive = lambda *args, **kwargs: hubspot.archives.append(args)
    orchestrator.run(
        SyncContext(trigger="poll", whatif=False, entities=["deals"]),
        aquira=FakeAquira(),
        hubspot=hubspot,
        catalog=catalog,
        existing=existing,
    )
    assert hubspot.archives == []


def test_skip_revenue_reassociates_deal():
    orchestrator = SyncOrchestrator()
    hubspot = FakeHubSpot()
    period_props = {
        "aquira_id": "42:2026-10:0",
        "period": "2026-10-01",
        "amount": 320.0,
        "spot_amount": 320.0,
        "charge_amount": 0,
        "source": "spot",
        "station": "KZBI-FM",
        "station_id": 0,
        "kind": "booked",
        "contract_cd": "1064",
        "deal_aquira_id": "42",
    }
    catalog = {
        "clients": [{"ID": 3, "Name": "Client", "IsAccount": True, "IsAdvertiser": True}],
        "contacts": [],
        "contracts": [
            {
                "ID": 42,
                "ContractCD": "1064",
                "IsContract": True,
                "Cancelled": False,
                "TotalValue": 320,
                "StartDate": "2026-10-01",
                "EndDate": "2026-10-31",
                "AccountID": 3,
                "AdvertiserID": 3,
                "lines": [{"station_id": 0, "station": "KZBI-FM", "start": "2026-10-01", "end": "2026-10-31", "amount": 320, "line_kind": "spot"}],
            }
        ],
        "reps": [],
    }
    existing = {
        "companies": [{"id": "hs-co-3", "properties": {"aquira_id": "3", "name": "Client"}}],
        "contacts": [],
        "deals": [{"id": "hs-deal-42", "properties": {"aquira_id": "42", "dealname": "1064 — Client", "amount": 320, "pipeline": "default", "dealstage": "closedwon"}}],
        "revenue": [{"id": "hs-rev-1", "properties": period_props}],
        "unsynced": [],
    }
    orchestrator.run(
        SyncContext(trigger="test", whatif=False, entities=["deals"]),
        aquira=FakeAquira(),
        hubspot=hubspot,
        catalog=catalog,
        existing=existing,
    )
    pairs = {(row[0], row[2], row[3]) for row in hubspot.associations}
    assert ("revenue_period", "deals", "hs-deal-42") in pairs
