from app.aquira.normalize import merge_contract, normalize_contract
from app.sync.planner import plan_deals


def test_normalize_contract_load_nested_refs_and_summary():
    payload = {
        "Success": True,
        "Entity": {
            "ID": 49,
            "Name": "49",
            "ContractCD": {"Value": "1070"},
            "IsContract": {"Value": True},
            "IsProposal": {"Value": False},
            "Status": {"Value": 1},
            "SignDate": {"Value": "2025-12-10T00:00:00"},
            "Advertiser": {"Value": {"ID": 202, "Name": "Park Cities Baptist"}},
            "Account": {"Value": {"ID": 101, "Name": "PCBC Agency"}},
            "SalesReps": {
                "Value": [
                    {
                        "SalesRepID": {"ID": 7, "SalesRepID": 7, "Name": "Jordan Reyes"},
                        "Selected": True,
                    }
                ]
            },
            "Summary": {
                "StartDate": "2026-01-01T00:00:00",
                "EndDate": "2026-03-31T00:00:00",
                "NetAmount": 12500,
                "GrossAmount": 14000,
            },
            "SpotLinesSummarized": {
                "Value": [
                    {
                        "FirstSpot": "2026-01-01T00:00:00",
                        "LastSpot": "2026-03-31T00:00:00",
                        "BookedTotalAmount": 12500,
                        "SelectedStationsCombined": [{"ID": 10, "Name": "KCBI"}],
                    }
                ]
            },
        },
    }

    contract = normalize_contract(payload)
    assert contract["ID"] == 49
    assert contract["ContractCD"] == "1070"
    assert contract["Name"] == "Park Cities Baptist"
    assert contract["AccountID"] == 101
    assert contract["AdvertiserID"] == 202
    assert contract["SalesRepID"] == 7
    assert contract["TotalValue"] == 12500
    assert contract["StartDate"] == "2026-01-01"
    assert contract["EndDate"] == "2026-03-31"
    assert contract["SignDate"] == "2025-12-10"
    assert contract["IsContract"] is True
    assert contract["Status"] == "Booked"
    assert contract["Stations"] == "KCBI"
    assert contract["lines"][0]["amount"] == 12500

    items = plan_deals([contract], {}, {"7": "hs-owner"}, {"202": "Park Cities Baptist"})
    props = items[0]["properties"]
    assert props["dealname"] == "1070 — Park Cities Baptist"
    assert props["amount"] == 12500
    assert props["aquira_account_id"] == "101"
    assert props["aquira_advertiser_id"] == "202"
    assert props["aquira_sales_rep"] == "7"
    assert props["hubspot_owner_id"] == "hs-owner"
    assert items[0]["associations"]["companyIds"] == ["101", "202"] or set(items[0]["associations"]["companyIds"]) == {
        "101",
        "202",
    }


def test_normalize_contract_search_row():
    row = {
        "ID": 49,
        "ContractCD": "1070",
        "Advertiser": {"ID": 202, "Name": "Park Cities Baptist", "IsAdvertiser": True},
        "Account": {"ID": 101, "Name": "PCBC Agency", "IsAccount": True},
        "SalesRep": {"ID": 7, "SalesRepID": 7, "Name": "Jordan Reyes"},
        "StartDate": "2026-01-01T00:00:00",
        "EndDate": "2026-03-31T00:00:00",
        "SignDate": "2025-12-10T00:00:00",
        "NetAmount": 12500,
        "Status": 2,
    }
    contract = normalize_contract(row)
    assert contract["AdvertiserID"] == 202
    assert contract["AccountID"] == 101
    assert contract["SalesRepID"] == 7
    assert contract["TotalValue"] == 12500
    assert contract["StartDate"] == "2026-01-01"
    assert contract["IsContract"] is True
    assert contract["Name"] == "Park Cities Baptist"


def test_merge_contract_keeps_search_amounts_when_load_is_thin():
    summary = normalize_contract(
        {
            "ID": 49,
            "ContractCD": "1070",
            "Advertiser": {"ID": 202, "Name": "Park Cities Baptist"},
            "Account": {"ID": 101, "Name": "Agency"},
            "SalesRep": {"ID": 7, "Name": "Jordan"},
            "StartDate": "2026-01-01",
            "EndDate": "2026-03-31",
            "NetAmount": 12500,
            "Status": 2,
        }
    )
    loaded = normalize_contract(
        {
            "Entity": {
                "ID": 49,
                "Name": "49",
                "ContractCD": {"Value": "1070"},
                "IsContract": {"Value": True},
                "Status": {"Value": 1},
                "SignDate": {"Value": "2025-12-10T00:00:00"},
            }
        }
    )
    merged = merge_contract(summary, loaded)
    assert merged["AdvertiserID"] == 202
    assert merged["TotalValue"] == 12500
    assert merged["StartDate"] == "2026-01-01"
    assert merged["IsContract"] is True
    assert merged["SignDate"] == "2025-12-10"
