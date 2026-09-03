from __future__ import annotations

from unittest.mock import MagicMock

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
