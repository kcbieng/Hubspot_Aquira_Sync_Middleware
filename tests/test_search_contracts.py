from unittest.mock import patch

from app.aquira.client import AquiraSessionClient
from app.aquira.normalize import normalize_client, normalize_contract
from app.sync.planner import company_properties, deal_properties, plan_deals


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


def test_load_spot_lines_does_not_hit_path_id_endpoints():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="x")
    seen: list[str] = []

    def fake_request(method, path, **kwargs):
        seen.append(path)
        if path == "/Contract/GetSpotLineDetailAnalysis":
            assert "json" in kwargs
            assert kwargs["json"]["id"] == 257
            return {"Success": True, "Data": {"Items": []}}
        if path == "/Contract/LoadSpotline":
            return {"Success": True, "SpotLine": {}}
        raise AssertionError(f"unexpected path {path}")

    with patch.object(client, "request", side_effect=fake_request):
        client.load_spot_lines(257, loaded={"Entity": {"ID": 257}})

    assert "/Contract/GetSpotLineDetailAnalysis/257" not in seen
    assert "/Contract/LoadSpotline/257" not in seen
    assert "/Contract/LoadSpotlineStationSpots" not in seen


def test_load_contract_prefers_monthly_analysis():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="x")

    def fake_request(method, path, **kwargs):
        if path == "/Contract/Load/257":
            return {
                "Success": True,
                "Entity": {"ID": 257, "ContractCD": {"Value": "1115"}, "IsContract": True, "TotalValue": 4500},
            }
        if path == "/Contract/GetContractDetailAnalysis":
            return {
                "Success": True,
                "Data": {
                    "Items": [
                        {
                            "StationShortName": "KCBI",
                            "Year": 2026,
                            "Month": 1,
                            "SpotGrossAmount": 4000,
                            "ChargeGrossAmount": 500,
                            "NetAmount": 4500,
                        }
                    ]
                },
            }
        raise AssertionError(f"unexpected path {path}")

    with patch.object(client, "request", side_effect=fake_request):
        contract = client.load_contract(257)

    kinds = {line["line_kind"] for line in contract["lines"]}
    assert kinds == {"spot", "charge"}
    assert sum(line["amount"] for line in contract["lines"]) == 4500


def test_resolve_clients_uses_client_cd_not_internal_id():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="x")

    def fake_request(method, path, **kwargs):
        if path == "/Client/Get":
            return {
                "Success": True,
                "Data": [
                    {"ID": 812, "ClientCD": "10043", "Name": "A New Beginning"},
                    {"ID": 43, "ClientCD": "10007", "Name": "Someone Else"},
                ],
            }
        if path == "/Client/Search":
            return {"Success": True, "Data": []}
        if path.startswith("/Client/Load/"):
            raise AssertionError(f"should not Load by ClientCD: {path}")
        return {"Success": True, "Data": []}

    with patch.object(client, "request", side_effect=fake_request):
        rows = client.resolve_clients("10043")

    assert len(rows) == 1
    assert rows[0]["ID"] == 812
    assert rows[0]["ClientCD"] == "10043"
    assert rows[0]["Name"] == "A New Beginning"


def test_normalize_client_keeps_client_cd():
    row = normalize_client({"ID": 812, "ClientCD": "10043", "Name": "A New Beginning", "IsAccount": True})
    assert row["ID"] == 812
    assert row["ClientCD"] == "10043"
    props = company_properties(row)
    assert props["aquira_id"] == "812"
    assert props["aquira_client_cd"] == "10043"
