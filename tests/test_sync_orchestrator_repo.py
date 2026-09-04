from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.sync.orchestrator import SyncContext, SyncOrchestrator


def test_orchestrator_creates_run_and_records_items():
    orchestrator = SyncOrchestrator()
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=99)

    result = orchestrator.run(SyncContext(trigger="manual", whatif=True, entities=["clients", "contracts"]), repo=repo)

    assert result["status"] == "success"
    assert result["run_id"] == 99
    assert result["entities"] == ["clients", "contracts"]
    assert repo.add_run.call_count == 1
    assert repo.add_event.call_count >= 1
    assert repo.add_run_item.call_count >= 1


def test_orchestrator_uses_default_company_contact_deal_sequence_when_unset():
    orchestrator = SyncOrchestrator()
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=7)

    result = orchestrator.run(SyncContext(trigger="scheduled", whatif=True), repo=repo)

    assert result["entities"] == ["companies", "contacts", "deals"]
    assert repo.add_run_item.call_count == 3
    assert repo.add_run_item.call_args_list[0].args[1] == "companies"
    assert repo.add_run_item.call_args_list[-1].args[1] == "deals"


def test_orchestrator_emits_live_payload_data_for_each_entity():
    orchestrator = SyncOrchestrator()
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=5)

    with patch("app.sync.orchestrator.AquiraSessionClient") as aquira_cls, patch("app.sync.orchestrator.HubSpotClient") as hubspot_cls:
        aquira_cls.return_value.load_sales_reps.return_value = [{"id": 12, "name": "Alpha Rep"}]
        hubspot_cls.return_value.get_owners.return_value = {"results": [{"ownerId": 77, "firstName": "Jamie", "lastName": "Smith"}]}

        orchestrator.run(SyncContext(trigger="manual", whatif=False, entities=["companies", "contacts"]), repo=repo)

    company_call = repo.add_run_item.call_args_list[0]
    contact_call = repo.add_run_item.call_args_list[1]
    assert company_call.kwargs["diff_json"]["source"] == "aquira"
    assert company_call.kwargs["diff_json"]["items"][0]["id"] == 12
    assert contact_call.kwargs["diff_json"]["source"] == "hubspot"
    assert contact_call.kwargs["diff_json"]["items"][0]["ownerId"] == 77


def test_orchestrator_uses_demo_payloads_when_live_sources_are_empty():
    orchestrator = SyncOrchestrator()
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=13)

    with patch("app.sync.orchestrator.AquiraSessionClient") as aquira_cls, patch("app.sync.orchestrator.HubSpotClient") as hubspot_cls:
        aquira_cls.return_value.load_sales_reps.return_value = []
        hubspot_cls.return_value.get_owners.return_value = {"results": []}

        orchestrator.run(SyncContext(trigger="manual", whatif=True, entities=["companies", "contacts", "deals"]), repo=repo)

    run_items = [call.kwargs["diff_json"] for call in repo.add_run_item.call_args_list]
    assert run_items[0]["items"]
    assert run_items[1]["items"]
    assert run_items[2]["items"]
    assert run_items[0]["items"][0]["demo"] is True


def test_manual_sync_api_uses_orchestrator_result_payload():
    from app.api.routes import run_sync

    result = run_sync({"whatif": False, "entities": ["companies"]})

    assert result["status"] == "success"
    assert result["entities"] == ["companies"]
    assert "run_id" in result
