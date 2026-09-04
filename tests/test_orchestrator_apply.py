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

    def create_client(self, fields):
        self.created.append(fields)
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
