from app.aquira.normalize import clients_from_contracts, merge_client, normalize_client
from app.mapping.parties import party_type_for_client
from app.sync.planner import company_properties


def test_normalize_client_uses_fullname_type_and_nested_address():
    payload = {
        "Success": True,
        "Entity": {
            "ID": 106,
            "Version": 10839706906243629056,
            "Name": "",
            "Fullname": {"Value": "Park Cities Baptist Church"},
            "Shortname": {"Value": "PCBC"},
            "Type": {"Value": 3},
            "BusinessPhone1": {"Value": "214-555-0100"},
            "Addresses": {
                "Value": {
                    "Physical": {
                        "Address": {"Name": "Address", "Value": {"Value": "6800 W Park Blvd"}, "DisplayOrder": 1},
                        "City": {"Name": "City", "Value": {"Value": "Plano"}, "DisplayOrder": 2},
                        "Region": {"Name": "State", "Value": {"Value": "TX"}, "DisplayOrder": 3},
                    }
                }
            },
        },
    }
    client = normalize_client(payload)
    assert client["Name"] == "Park Cities Baptist Church"
    assert client["Phone"] == "214-555-0100"
    assert client["PhysicalAddress"] == "6800 W Park Blvd"
    assert client["City"] == "Plano"
    assert client["State"] == "TX"
    assert client["IsAccount"] is True
    assert client["IsAdvertiser"] is True
    assert party_type_for_client(client) == "both"
    props = company_properties(client)
    assert props["name"] == "Park Cities Baptist Church"
    assert props["aquira_party_type"] == "both"


def test_merge_client_keeps_search_name_when_load_is_thin():
    summary = normalize_client({"ID": 106, "Name": "Park Cities Baptist", "Type": 1, "BusinessPhone1": "214-555-0100"})
    loaded = normalize_client({"Entity": {"ID": 106, "Version": 99, "Name": ""}})
    merged = merge_client(summary, loaded)
    assert merged["Name"] == "Park Cities Baptist"
    assert merged["IsAccount"] is True
    assert merged["Phone"] == "214-555-0100"


def test_clients_from_contracts_seed_account_and_advertiser():
    rows = clients_from_contracts(
        [
            {
                "ID": 85,
                "AccountID": 101,
                "AdvertiserID": 106,
                "AccountName": "PCBC Agency",
                "AdvertiserName": "Park Cities Baptist",
                "Name": "Park Cities Baptist",
            }
        ]
    )
    by_id = {row["ID"]: row for row in rows}
    assert by_id[101]["IsAccount"] is True
    assert by_id[101]["Name"] == "PCBC Agency"
    assert by_id[106]["IsAdvertiser"] is True
    assert by_id[106]["Name"] == "Park Cities Baptist"
