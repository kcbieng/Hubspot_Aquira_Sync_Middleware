from app.services.sync_service import SyncPlanner


def test_sync_planner_returns_field_diff_list_for_client_updates():
    planner = SyncPlanner()
    current = {"ID": 7, "Name": {"Value": "Acme", "Access": 2}, "Email": {"Value": "old@example.com", "Access": 2}}
    proposed = {"ID": 7, "Name": {"Value": "Acme Labs", "Access": 2}, "Email": {"Value": "old@example.com", "Access": 2}}

    plan = planner.plan_client_update(7, current, proposed)

    assert plan["entity"] == "client"
    assert plan["action"] == "update"
    assert plan["keys"] == {"aquira_id": 7}
    assert plan["field_diff"] == [{"field": "Name", "from": "Acme", "to": "Acme Labs"}]


def test_sync_planner_returns_list_for_client_create():
    planner = SyncPlanner()
    plan = planner.plan_client_create({"ID": 11, "Name": "New Co", "Email": "new@example.com"})

    assert plan["action"] == "create"
    assert plan["keys"] == {"aquira_id": 11}
    assert plan["field_diff"][0]["field"] == "Name"


def test_sync_planner_builds_company_plan_for_account_advertiser_link():
    planner = SyncPlanner()
    account = {"ID": {"Value": 101}, "Name": {"Value": "Acme Holdings"}, "IsAccount": {"Value": True}, "IsAdvertiser": {"Value": False}}
    advertiser = {"ID": {"Value": 202}, "Name": {"Value": "Acme Media"}, "IsAccount": {"Value": False}, "IsAdvertiser": {"Value": True}}

    plan = planner.plan_company_upsert(account, advertiser)

    assert plan["entity"] == "company"
    assert plan["action"] == "upsert"
    assert plan["aquira_party_type"] == "account"
    assert plan["keys"] == {"aquira_id": 101}
    assert plan["account_id"] == 101
    assert plan["advertiser_id"] == 202
    assert plan["needs_parent"] is True
