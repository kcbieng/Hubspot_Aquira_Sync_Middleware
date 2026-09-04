from app.api.routes import owner_map
from app.mapping.owners import suggest_owner_map


def test_owner_suggest_prefers_email_then_name():
    aquira_reps = [
        {"id": 1, "name": "Jane Smith", "email": "jane@acme.example"},
        {"id": 2, "name": "Bob Jones", "email": "bob@other.example"},
        {"id": 3, "name": "Casey White", "email": ""},
    ]
    hubspot_owners = [
        {"owner_id": "hs-1", "name": "Jane Smith", "email": "jane@acme.example"},
        {"owner_id": "hs-2", "name": "Bobby Jones", "email": "bobby@other.example"},
        {"owner_id": "hs-3", "name": "Casey White", "email": "casey@fresh.example"},
    ]

    suggestions = suggest_owner_map(aquira_reps, hubspot_owners)
    assert suggestions[0]["hubspot_owner_id"] == "hs-1"
    assert suggestions[1]["hubspot_owner_id"] == "hs-2"
    assert suggestions[2]["hubspot_owner_id"] == "hs-3"
    assert suggestions[0]["suggested"] is True


def test_owner_map_builds_live_aquira_to_hubspot_mapping(monkeypatch):
    class FakeAquira:
        def __init__(self, *args, **kwargs):
            pass

        def load_sales_reps(self):
            return [
                {"id": 1, "name": "Jane Smith", "email": "jane@acme.example"},
                {"id": 2, "name": "Bob Jones", "email": "bob@other.example"},
            ]

    class FakeHubSpot:
        def get_owners(self):
            return {
                "results": [
                    {"ownerId": "hs-1", "email": "jane@acme.example", "name": "Jane Smith"},
                    {"ownerId": "hs-2", "email": "bobby@other.example", "name": "Bobby Jones"},
                ]
            }

    monkeypatch.setattr("app.api.routes.AquiraSessionClient", FakeAquira)
    monkeypatch.setattr("app.api.routes.HubSpotClient", FakeHubSpot)

    mapping = owner_map()
    assert mapping[0]["aquira_user_id"] == 1
    assert mapping[0]["hubspot_owner_id"] == "hs-1"
    assert mapping[0]["enabled"] is True
    assert mapping[1]["hubspot_owner_id"] == "hs-2"
