from app.sync.whatif import WhatIfPlanner


def test_webhook_dedupe_stub():
    planner = WhatIfPlanner()
    assert planner.plan_skip("webhook", "duplicate")['action'] == 'skip'
