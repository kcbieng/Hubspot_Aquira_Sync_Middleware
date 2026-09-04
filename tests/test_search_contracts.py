from unittest.mock import patch

from app.aquira.client import AquiraSessionClient
from app.aquira.normalize import normalize_contract
from app.sync.planner import deal_properties, plan_deals


def test_search_contracts_unions_lookup_and_get_proposals():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="x")
    search = {
        "Success": True,
        "Data": [{"ID": 49, "ContractCD": "1070", "IsContract": True, "IsProposal": False, "Status": 2}],
    }
    lookup = {
        "Success": True,
        "Data": [
            {"ID": 49, "Name": "1070"},
            {"ID": 85, "Name": "Spring Promo", "Status": 1},
        ],
    }
    get_all = {"Success": True, "Data": [{"ID": 90, "Status": 1, "Description": "Q1 proposal"}]}

    def fake_request(method, path, **kwargs):
        if path == "/Contract/Search":
            return search
        if path == "/Contract/Lookup":
            assert kwargs["json"]["IncludeStatuses"] == [0, 1, 2, 3, 4, 5]
            return lookup
        if path == "/Contract/Get":
            return get_all
        return {"Success": True, "Data": []}

    with patch.object(client, "request", side_effect=fake_request):
        rows = client.search_contracts()

    by_id = {row["ID"]: row for row in rows}
    assert set(by_id) == {49, 85, 90}
    assert by_id[49]["IsContract"] is True
    assert by_id[85]["IsProposal"] is True
    assert by_id[85]["IsContract"] is False
    assert by_id[90]["Name"] == "Q1 proposal"
    assert by_id[90]["IsProposal"] is True


def test_normalize_proposal_status_without_flags():
    contract = normalize_contract(
        {
            "ID": 85,
            "ContractCD": "1115",
            "Description": "Underwriting flight",
            "Status": {"Value": 1},
            "IsProposal": {"Value": True},
            "IsContract": {"Value": False},
        }
    )
    assert contract["IsProposal"] is True
    assert contract["IsContract"] is False
    assert contract["Status"] == "Proposal"
    assert contract["Name"] == "Underwriting flight"
    props = deal_properties(contract)
    assert props["dealstage"] == "proposal"
    assert props["aquira_is_proposal"] is True
    assert props["aquira_is_contract"] is False
    items = plan_deals([contract], {}, {}, {})
    assert items[0]["properties"]["dealstage"] == "proposal"


def test_normalize_status_name_proposal():
    contract = normalize_contract({"ID": 12, "Status": {"ID": 1, "Name": "Proposal"}})
    assert contract["IsProposal"] is True
    assert contract["IsContract"] is False
    assert contract["Name"] == "Proposal 12"
