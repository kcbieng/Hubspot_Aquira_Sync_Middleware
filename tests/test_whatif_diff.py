from app.sync.whatif import WhatIfPlanner, diff_props


def test_diff_props_and_plan_update():
    old = {"Name": "Old", "Email": "old@example.com"}
    new = {"Name": "New", "Email": "old@example.com"}
    diffs = diff_props(old, new)
    assert diffs == [{"field": "Name", "from": "Old", "to": "New"}]

    planner = WhatIfPlanner()
    plan = planner.plan_update("client", "id", {"id": 5, "Name": "Old"}, {"id": 5, "Name": "New"})
    assert plan["action"] == "update"
    assert plan["field_diff"][0]["field"] == "Name"
