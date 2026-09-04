from app.api.routes import owner_map
from app.mapping.owners import classify_hubspot_user, expand_owner_lookup, suggest_owner_map
from app.aquira.normalize import normalize_rep


def test_owner_suggest_prefers_email_then_name():
    aquira_reps = [
        {"id": 1, "name": "Jane Smith", "email": "jane@acme.example"},
        {"id": 2, "name": "Bob Jones", "email": "bob@other.example"},
        {"id": 3, "name": "Casey White", "email": ""},
    ]
    hubspot_owners = [
        {"owner_id": "hs-1", "name": "Jane Smith", "email": "jane@acme.example", "kind": "sales"},
        {"owner_id": "hs-2", "name": "Bobby Jones", "email": "bobby@other.example", "kind": "sales"},
        {"owner_id": "hs-3", "name": "Casey White", "email": "casey@fresh.example", "kind": "user"},
    ]

    suggestions = suggest_owner_map(aquira_reps, hubspot_owners)
    assert suggestions[0]["hubspot_owner_id"] == "hs-1"
    assert suggestions[1]["hubspot_owner_id"] == "hs-2"
    assert suggestions[2]["hubspot_owner_id"] == "hs-3"
    assert suggestions[0]["suggested"] is True


def test_owner_suggest_does_not_map_name_onto_super_admin():
    suggestions = suggest_owner_map(
        [{"id": 4, "name": "Pat Admin", "email": ""}],
        [
            {"owner_id": "hs-admin", "name": "Pat Admin", "email": "admin@kcbi.org", "kind": "admin", "super_admin": True, "role": "Super Admin"},
            {"owner_id": "hs-sales", "name": "Pat Sales", "email": "pat@kcbi.org", "kind": "sales", "role": "Sales"},
        ],
    )
    assert suggestions[0]["hubspot_owner_id"] is None
    assert suggestions[0]["enabled"] is False


def test_owner_suggest_allows_super_admin_only_on_exact_email():
    suggestions = suggest_owner_map(
        [{"id": 5, "name": "Pat Admin", "email": "admin@kcbi.org"}],
        [
            {"owner_id": "hs-admin", "name": "Pat Admin", "email": "admin@kcbi.org", "kind": "admin", "super_admin": True, "role": "Super Admin"},
        ],
    )
    assert suggestions[0]["hubspot_owner_id"] == "hs-admin"


def test_classify_hubspot_user_roles():
    assert classify_hubspot_user("Super Admin", False) == "admin"
    assert classify_hubspot_user("Sales Manager", False) == "sales"
    assert classify_hubspot_user("Marketing", True) == "admin"
    assert classify_hubspot_user("Coordinator", False) == "user"


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

        def list_sales_users(self):
            return [
                {"owner_id": "hs-1", "name": "Jane Smith", "email": "jane@acme.example", "kind": "sales", "role": "Sales"},
                {"owner_id": "hs-2", "name": "Bobby Jones", "email": "bobby@other.example", "kind": "sales", "role": "Sales"},
            ]

    monkeypatch.setattr("app.api.routes.AquiraSessionClient", FakeAquira)
    monkeypatch.setattr("app.api.routes.HubSpotClient", FakeHubSpot)

    mapping = owner_map()
    assert mapping[0]["aquira_user_id"] == 1
    assert mapping[0]["hubspot_owner_id"] == "hs-1"
    assert mapping[0]["enabled"] is True
    assert mapping[1]["hubspot_owner_id"] == "hs-2"


def test_normalize_rep_keeps_user_id_and_sales_rep_id():
    rep = normalize_rep({"ID": 123, "SalesRepID": 4, "Name": "Clint Lewis", "Email": "clint@kcbi.org"})
    assert rep["id"] == "123"
    assert rep["user_id"] == "123"
    assert rep["sales_rep_id"] == "4"


def test_expand_owner_lookup_aliases_contract_sales_rep_id():
    lookup = expand_owner_lookup(
        [{"aquira_user_id": "123", "aquira_name": "Clint Lewis", "hubspot_owner_id": "hs-clint", "enabled": True}],
        [{"id": "123", "user_id": "123", "sales_rep_id": "4", "name": "Clint Lewis"}],
    )
    assert lookup["123"] == "hs-clint"
    assert lookup["4"] == "hs-clint"

