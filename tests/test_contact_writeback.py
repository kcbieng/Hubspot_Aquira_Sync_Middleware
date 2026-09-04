from app.sync.orchestrator import SyncOrchestrator
from app.sync.planner import plan_contact_writebacks


class FakeAquira:
    def __init__(self):
        self.contact_puts = []
        self.puts = []

    def update_client_sparse(self, aquira_id, fields):
        self.puts.append((aquira_id, fields))
        return {"Success": True}

    def update_contact_sparse(self, client_id, contact_id, fields):
        self.contact_puts.append((client_id, contact_id, fields))
        return {"Success": True}


def test_plan_contact_writeback_includes_client_id():
    items = plan_contact_writebacks(
        [
            {
                "aquira_id": "501",
                "hubspotId": "ct-1",
                "properties": {"firstname": "Carl", "lastname": "Sewell", "email": "carl@sewell.com", "phone": "2145551111"},
            }
        ],
        {"501": {"ID": 501, "ClientID": 101, "FirstName": "Carl", "LastName": "Sewell", "Email": "carl@sewell.com", "Phone": "2145550000"}},
    )
    assert items[0]["writeback"] is True
    assert items[0]["associations"]["clientId"] == 101
    assert items[0]["properties"]["Phone"] == "2145551111"


def test_apply_item_writes_nested_contact_not_client_email():
    aquira = FakeAquira()
    item = {
        "entityType": "contact",
        "aquiraId": "501",
        "hubspotId": "ct-1",
        "action": "update",
        "writeback": True,
        "properties": {"FirstName": "Carl", "LastName": "Sewell", "Email": "carl@sewell.com", "Phone": "2145551111"},
        "associations": {"clientId": 101},
    }
    SyncOrchestrator().apply_item(item, aquira, None, {})
    assert aquira.contact_puts == [
        (101, "501", {"FirstName": "Carl", "LastName": "Sewell", "Email": "carl@sewell.com", "Phone": "2145551111"})
    ]
    assert aquira.puts == []
