from __future__ import annotations

from unittest.mock import patch

import pytest

from app.hubspot.client import HubSpotClient


def test_hubspot_ensure_properties_creates_missing_fields():
    client = HubSpotClient(access_token="token")

    with patch.object(client, "get_properties", return_value={"results": [{"name": "domain"}]}) as get_mock:
        with patch.object(client, "create_property") as create_mock:
            created = client.ensure_properties("company", ["domain", "aquira_id", "aquira_party_type"])

    assert created == ["aquira_id", "aquira_party_type"]
    assert get_mock.call_count == 1
    assert create_mock.call_count == 2


def test_hubspot_validate_required_properties_raises_clear_error_when_missing():
    client = HubSpotClient(access_token="token")

    with patch.object(client, "get_properties", return_value={"results": [{"name": "domain"}]}) :
        with pytest.raises(ValueError, match="Missing required HubSpot company properties: aquira_id, aquira_party_type"):
            client.validate_required_properties("company", ["domain", "aquira_id", "aquira_party_type"])


def test_hubspot_builds_company_payload_from_aquira_client():
    client = HubSpotClient(access_token="token")
    aquira_client = {
        "ID": {"Value": 101},
        "Name": {"Value": "Acme Holdings"},
        "Domain": {"Value": "acme.com"},
        "Phone": {"Value": "555-0100"},
        "IsAccount": {"Value": True},
        "IsAdvertiser": {"Value": False},
    }

    payload = client.build_company_payload(aquira_client)

    assert payload["properties"]["name"] == "Acme Holdings"
    assert payload["properties"]["domain"] == "acme.com"
    assert payload["properties"]["phone"] == "555-0100"
    assert payload["properties"]["aquira_id"] == 101
    assert payload["properties"]["aquira_party_type"] == "account"


def test_hubspot_upsert_company_creates_missing_record_and_links_parent():
    client = HubSpotClient(access_token="token")
    aquira_client = {
        "ID": {"Value": 101},
        "Name": {"Value": "Acme Holdings"},
        "Domain": {"Value": "acme.com"},
        "Phone": {"Value": "555-0100"},
        "IsAccount": {"Value": True},
        "IsAdvertiser": {"Value": False},
    }

    search_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"results": []}})()
    create_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"id": "cmp-999"}})()
    association_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"status": "ok"}})()

    with patch("app.hubspot.client.httpx.post", side_effect=[search_response, create_response, association_response]) as post_mock:
        result = client.upsert_company(aquira_client, parent_company_id="cmp-111")

    assert result["id"] == "cmp-999"
    assert post_mock.call_count == 3
    assert post_mock.call_args_list[0].kwargs["json"]["filterGroups"][0]["filters"][0]["value"] == "101"
    assert post_mock.call_args_list[2].kwargs["json"]["inputs"][0]["to"]["id"] == "cmp-111"


def test_hubspot_upsert_contact_creates_missing_record_by_email():
    client = HubSpotClient(access_token="token")
    aquira_contact = {
        "ID": {"Value": 302},
        "FirstName": {"Value": "Jane"},
        "LastName": {"Value": "Doe"},
        "Email": {"Value": "jane@example.com"},
        "Phone": {"Value": "555-1212"},
    }

    search_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"results": []}})()
    create_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"id": "ct-88"}})()
    association_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"status": "ok"}})()

    with patch("app.hubspot.client.httpx.post", side_effect=[search_response, create_response, association_response]) as post_mock:
        result = client.upsert_contact(aquira_contact, associated_company_ids=["cmp-111"])

    assert result["id"] == "ct-88"
    assert post_mock.call_count == 3
    assert post_mock.call_args_list[0].kwargs["json"]["filterGroups"][0]["filters"][0]["value"] == "302"
    assert post_mock.call_args_list[1].kwargs["json"]["properties"]["email"] == "jane@example.com"
    assert post_mock.call_args_list[2].kwargs["json"]["inputs"][0]["to"]["id"] == "cmp-111"


def test_hubspot_builds_deal_payload_and_revenue_periods_from_contract():
    client = HubSpotClient(access_token="token")
    contract = {
        "Entity": {
            "ID": 9001,
            "ContractCD": {"Value": "C-9001"},
            "Name": {"Value": "Spring Campaign"},
            "StartDate": {"Value": "2026-01-15"},
            "EndDate": {"Value": "2026-03-15"},
            "TotalValue": {"Value": 12000},
            "IsProposal": {"Value": False},
            "IsContract": {"Value": True},
            "AdvertiserID": {"Value": 202},
            "AccountID": {"Value": 101},
        }
    }
    raw_detail = {
        "lines": [
            {
                "StartDate": {"Value": "2026-01-15"},
                "EndDate": {"Value": "2026-03-15"},
                "Amount": {"Value": 12000},
                "StationID": {"Value": 10},
                "Station": {"Value": {"ID": 10, "Name": "KCBI"}},
            }
        ]
    }

    deal_payload = client.build_deal_payload(contract)
    revenue_periods = client.build_revenue_period_payloads(contract, raw_detail, deal_id="deal-1", company_ids=["cmp-111", "cmp-222"])

    assert deal_payload["properties"]["aquira_id"] == 9001
    assert deal_payload["properties"]["aquira_contract_cd"] == "C-9001"
    assert deal_payload["properties"]["aquira_is_contract"] is True
    assert revenue_periods[0]["properties"]["aquira_id"] == "9001:2026-01:10"
    assert revenue_periods[0]["properties"]["amount"] == 4000.0


def test_hubspot_upsert_deal_creates_missing_record_and_associates_companies():
    client = HubSpotClient(access_token="token")
    contract = {
        "Entity": {
            "ID": 9005,
            "ContractCD": {"Value": "C-9005"},
            "Name": {"Value": "Summer Campaign"},
            "StartDate": {"Value": "2026-06-01"},
            "EndDate": {"Value": "2026-08-31"},
            "TotalValue": {"Value": 6000},
            "IsProposal": {"Value": False},
            "IsContract": {"Value": True},
            "AdvertiserID": {"Value": 202},
            "AccountID": {"Value": 101},
        }
    }

    search_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"results": []}})()
    create_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"id": "deal-42"}})()
    account_assoc = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"status": "ok"}})()
    advertiser_assoc = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"status": "ok"}})()

    with patch("app.hubspot.client.httpx.post", side_effect=[search_response, create_response, account_assoc, advertiser_assoc]) as post_mock:
        result = client.upsert_deal(contract, account_company_id="cmp-111", advertiser_company_id="cmp-222")

    assert result["id"] == "deal-42"
    assert post_mock.call_count == 4
    assert post_mock.call_args_list[0].kwargs["json"]["filterGroups"][0]["filters"][0]["value"] == "9005"
    assert post_mock.call_args_list[1].kwargs["json"]["properties"]["aquira_id"] == 9005
    assert post_mock.call_args_list[2].kwargs["json"]["inputs"][0]["to"]["id"] == "cmp-111"
    assert post_mock.call_args_list[3].kwargs["json"]["inputs"][0]["to"]["id"] == "cmp-222"
