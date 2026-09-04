from app.aquira.normalize import extract_attributes, normalize_client, normalize_contact
from app.mapping.teams import apply_team_ids, collect_team_keys, suggest_team_map
from app.sync.planner import company_properties, contact_properties, plan_companies, plan_contacts, plan_deals


def test_extract_attributes_from_field_values():
    attrs = extract_attributes(
        {
            "Attributes": [
                {"Name": "HubSpot Team", "Value": "KCBI Sales"},
                {"Name": "Other", "Value": {"Name": "ignored"}},
            ]
        }
    )
    assert attrs["HubSpot Team"] == "KCBI Sales"


def test_extract_attributes_from_already_normalized_dict():
    attrs = extract_attributes({"Attributes": {"HubSpot Team": "KCBI Sales"}})
    assert attrs["HubSpot Team"] == "KCBI Sales"


def test_suggest_team_map_matches_exact_name():
    suggestions = suggest_team_map(
        [{"aquira_key": "kcbi sales", "aquira_label": "KCBI Sales", "source": "attribute", "count": 3}],
        [{"id": "t-1", "name": "KCBI Sales"}, {"id": "t-2", "name": "KLTY Digital"}],
    )
    assert suggestions[0]["hubspot_team_id"] == "t-1"
    assert suggestions[0]["enabled"] is True
    assert suggestions[0]["suggested"] is True


def test_contacts_inherit_client_team_then_contract():
    catalog = {
        "clients": [
            {
                "ID": 106,
                "Name": "Client 106",
                "Attributes": {"HubSpot Team": "KCBI Sales"},
                "IsAccount": True,
                "IsAdvertiser": True,
            }
        ],
        "contacts": [{"ID": 9, "ClientID": 106, "FirstName": "Pat", "LastName": "Seller", "Attributes": {}}],
        "contracts": [
            {
                "ID": 85,
                "AccountID": 106,
                "AdvertiserID": 106,
                "Stations": "KCBI",
                "Attributes": {},
            }
        ],
    }
    apply_team_ids(catalog, {}, teams_by_name={"kcbi sales": "t-1"})
    assert catalog["clients"][0]["hubspot_team_id"] == "t-1"
    assert catalog["contacts"][0]["hubspot_team_id"] == "t-1"
    assert catalog["contracts"][0]["hubspot_team_id"] == "t-1"
    props = contact_properties(catalog["contacts"][0])
    assert props["hubspot_team_id"] == "t-1"
    items = plan_contacts(catalog["contacts"], {}, {})
    assert items[0]["properties"]["hubspot_team_id"] == "t-1"
    company_items = plan_companies(catalog["clients"], {})
    assert company_items[0]["properties"]["hubspot_team_id"] == "t-1"
    assert company_properties(catalog["clients"][0])["hubspot_team_id"] == "t-1"


def test_hubspot_team_lookup_attribute_matches_program_support():
    from app.aquira.normalize import normalize_client
    from app.mapping.teams import team_label_from

    payload = {
        "Entity": {
            "ID": 44,
            "ClientCD": {"Value": "10043"},
            "Fullname": {"Value": "A New Beginning"},
            "Attributes": {
                "Value": [
                    {
                        "Name": "Hubspot_Team",
                        "AttrType": "LookUp",
                        "Value": {
                            "IsCurrent": True,
                            "DisplayOrder": 5,
                            "Name": "Program Support",
                            "LongName": "Program Support",
                            "ID": 14,
                        },
                    }
                ]
            },
        }
    }
    client = normalize_client(payload)
    assert client["Attributes"]["Hubspot_Team"] == "Program Support"
    assert team_label_from(client) == "Program Support"
    catalog = {
        "clients": [client],
        "contacts": [{"ID": 9, "ClientID": 44, "FirstName": "Pat", "LastName": "Seller", "Attributes": {}}],
        "contracts": [],
    }
    apply_team_ids(catalog, {}, teams_by_name={"program support": "t-ps"})
    assert catalog["clients"][0]["hubspot_team_id"] == "t-ps"
    assert catalog["contacts"][0]["hubspot_team_id"] == "t-ps"
    assert company_properties(catalog["clients"][0])["hubspot_team_id"] == "t-ps"
    assert company_properties(catalog["clients"][0])["aquira_hubspot_team"] == "Program Support"
    assert contact_properties(catalog["contacts"][0])["hubspot_team_id"] == "t-ps"


def test_team_default_owner_fills_when_sales_rep_is_unmapped():
    catalog = {
        "clients": [
            {
                "ID": 44,
                "Name": "A New Beginning",
                "Attributes": {"Hubspot_Team": "Program Support"},
            }
        ],
        "contacts": [{"ID": 9, "ClientID": 44, "FirstName": "Pat", "LastName": "Seller", "Attributes": {}}],
        "contracts": [{"ID": 1, "AccountID": 44, "AdvertiserID": 44, "Attributes": {}}],
    }
    apply_team_ids(
        catalog,
        {},
        teams_by_name={"program support": "t-ps"},
        team_owner_by_team_id={"t-ps": "hs-queue"},
    )
    assert catalog["clients"][0]["hubspot_owner_id"] == "hs-queue"
    assert catalog["contacts"][0]["hubspot_owner_id"] == "hs-queue"
    assert catalog["contracts"][0]["hubspot_owner_id"] == "hs-queue"
    assert company_properties(catalog["clients"][0])["hubspot_owner_id"] == "hs-queue"
    assert contact_properties(catalog["contacts"][0])["hubspot_owner_id"] == "hs-queue"


def test_mapped_sales_rep_beats_team_queue_owner():
    catalog = {
        "clients": [
            {
                "ID": 1,
                "Name": "Adv",
                "SalesRepID": 7,
                "Attributes": {"HubSpot Team": "Program Support"},
            }
        ],
        "contacts": [],
        "contracts": [],
    }
    apply_team_ids(
        catalog,
        {},
        teams_by_name={"program support": "t-ps"},
        owner_by_aquira={"7": "hs-jane"},
        team_owner_by_team_id={"t-ps": "hs-queue"},
    )
    assert catalog["clients"][0]["hubspot_owner_id"] == "hs-jane"


def test_company_inherits_owner_from_contract_sales_rep():
    catalog = {
        "clients": [{"ID": 44, "Name": "A New Beginning", "Attributes": {}}],
        "contacts": [{"ID": 9, "ClientID": 44, "FirstName": "Pat", "LastName": "Seller", "Attributes": {}}],
        "contracts": [
            {
                "ID": 1,
                "AccountID": 44,
                "AdvertiserID": 44,
                "SalesRepID": 7,
                "SalesRepName": "Clint Lewis",
                "Attributes": {},
            }
        ],
    }
    apply_team_ids(catalog, {}, owner_by_aquira={"7": "hs-clint"})
    assert catalog["contracts"][0]["hubspot_owner_id"] == "hs-clint"
    assert catalog["clients"][0]["hubspot_owner_id"] == "hs-clint"
    assert catalog["contacts"][0]["hubspot_owner_id"] == "hs-clint"
    assert company_properties(catalog["clients"][0])["hubspot_owner_id"] == "hs-clint"


def test_company_owner_matches_sales_rep_name():
    catalog = {
        "clients": [{"ID": 44, "Name": "A New Beginning", "SalesRepName": "Clint Lewis", "Attributes": {}}],
        "contacts": [],
        "contracts": [],
    }
    apply_team_ids(catalog, {}, owner_by_name={"clint lewis": "hs-clint"})
    assert catalog["clients"][0]["hubspot_owner_id"] == "hs-clint"



def test_station_fallback_maps_contract_team():
    catalog = {
        "clients": [],
        "contacts": [],
        "contracts": [{"ID": 1, "Stations": "KCBI", "Attributes": {}, "IsProposal": True}],
    }
    apply_team_ids(catalog, {"kcbi": "t-station"})
    assert catalog["contracts"][0]["hubspot_team_id"] == "t-station"
    items = plan_deals(catalog["contracts"], {}, {})
    assert items[0]["properties"]["hubspot_team_id"] == "t-station"


def test_advertiser_name_maps_when_no_attribute():
    catalog = {
        "clients": [{"ID": 2, "Name": "Unlabeled Advertiser", "IsAdvertiser": True, "Attributes": {}}],
        "contacts": [{"ID": 9, "ClientID": 2, "FirstName": "Pat", "LastName": "Seller", "Attributes": {}}],
        "contracts": [{"ID": 1, "AdvertiserID": 2, "AccountID": 2, "Stations": "", "Attributes": {}}],
    }
    apply_team_ids(catalog, {"unlabeled advertiser": "t-adv"})
    assert catalog["clients"][0]["hubspot_team_id"] == "t-adv"
    assert catalog["contacts"][0]["hubspot_team_id"] == "t-adv"
    assert catalog["contracts"][0]["hubspot_team_id"] == "t-adv"


def test_advertiser_name_does_not_auto_match_hubspot_team():
    catalog = {
        "clients": [{"ID": 2, "Name": "KCBI Sales", "IsAdvertiser": True, "Attributes": {}}],
        "contacts": [],
        "contracts": [],
    }
    apply_team_ids(catalog, {}, teams_by_name={"kcbi sales": "t-1"})
    assert catalog["clients"][0]["hubspot_team_id"] is None


def test_child_advertiser_inherits_account_team():
    catalog = {
        "clients": [
            {"ID": 1, "Name": "Account Co", "IsAccount": True, "Attributes": {"HubSpot Team": "KLTY Digital"}},
            {"ID": 2, "Name": "Adv", "IsAdvertiser": True, "AccountID": 1, "Attributes": {}},
        ],
        "contacts": [{"ID": 9, "ClientID": 2, "FirstName": "A", "LastName": "B", "Attributes": {}}],
        "contracts": [],
    }
    apply_team_ids(catalog, {}, teams_by_name={"klty digital": "t-2"})
    assert catalog["clients"][0]["hubspot_team_id"] == "t-2"
    assert catalog["clients"][1]["hubspot_team_id"] == "t-2"
    assert catalog["contacts"][0]["hubspot_team_id"] == "t-2"


def test_contact_attribute_wins_over_client():
    catalog = {
        "clients": [{"ID": 1, "Name": "Account", "Attributes": {"HubSpot Team": "KCBI Sales"}}],
        "contacts": [{"ID": 9, "ClientID": 1, "FirstName": "Pat", "LastName": "Seller", "Attributes": {"HubSpot Team": "KLTY Digital"}}],
        "contracts": [],
    }
    apply_team_ids(catalog, {}, teams_by_name={"kcbi sales": "t-1", "klty digital": "t-2"})
    assert catalog["contacts"][0]["hubspot_team_id"] == "t-2"


def test_collect_team_keys_includes_attribute_and_advertiser():
    keys = collect_team_keys(
        {
            "clients": [
                {"ID": 1, "Name": "Park Cities", "IsAdvertiser": True, "Attributes": {"HubSpot Team": "KCBI Sales"}},
                {"ID": 2, "Name": "Unlabeled Advertiser", "IsAdvertiser": True, "Attributes": {}},
            ],
            "contracts": [{"ID": 3, "Stations": "KLTY", "Attributes": {}}],
        }
    )
    labels = {row["aquira_label"]: row["source"] for row in keys}
    assert labels["KCBI Sales"] == "attribute"
    assert labels["Unlabeled Advertiser"] == "advertiser"
    assert labels["KLTY"] == "station"


def test_product_code_maps_contract_when_unique():
    catalog = {
        "clients": [],
        "contacts": [],
        "contracts": [
            {
                "ID": 1,
                "Stations": "",
                "Attributes": {},
                "ProductNames": ["KCBI-AM"],
                "lines": [{"products": ["KCBI-AM"]}],
            }
        ],
    }
    apply_team_ids(catalog, {"product:kcbi am": "t-product"})
    assert catalog["contracts"][0]["hubspot_team_id"] == "t-product"


def test_conflicting_product_codes_do_not_assign():
    catalog = {
        "clients": [],
        "contacts": [],
        "contracts": [{"ID": 1, "Attributes": {}, "ProductNames": ["KCBI-AM", "KLTY-FM"]}],
    }
    apply_team_ids(catalog, {"product:kcbi am": "t-1", "product:klty fm": "t-2"})
    assert catalog["contracts"][0]["hubspot_team_id"] is None


def test_sales_rep_map_assigns_team():
    catalog = {
        "clients": [{"ID": 2, "Name": "Adv", "IsAdvertiser": True, "SalesRepID": 7, "SalesRepName": "Jane Doe", "Attributes": {}}],
        "contacts": [{"ID": 9, "ClientID": 2, "FirstName": "Pat", "LastName": "Seller", "Attributes": {}}],
        "contracts": [{"ID": 1, "AdvertiserID": 2, "AccountID": 2, "SalesRepID": 7, "SalesRepName": "Jane Doe", "Attributes": {}}],
    }
    apply_team_ids(catalog, {"salesrep:jane doe": "t-rep"})
    assert catalog["clients"][0]["hubspot_team_id"] == "t-rep"
    assert catalog["contacts"][0]["hubspot_team_id"] == "t-rep"
    assert catalog["contracts"][0]["hubspot_team_id"] == "t-rep"


def test_sales_rep_uses_mapped_owner_primary_team():
    catalog = {
        "clients": [],
        "contacts": [],
        "contracts": [{"ID": 1, "SalesRepID": 7, "SalesRepName": "Jane Doe", "Attributes": {}}],
    }
    apply_team_ids(catalog, {}, owner_by_aquira={"7": "hs-jane"}, owner_team_by_owner_id={"hs-jane": "t-owner"})
    assert catalog["contracts"][0]["hubspot_team_id"] == "t-owner"


def test_aquira_sales_team_matches_hubspot_team_name():
    catalog = {
        "clients": [{"ID": 2, "Name": "Adv", "IsAdvertiser": True, "SalesTeams": ["KCBI Sales"], "Attributes": {}}],
        "contacts": [{"ID": 9, "ClientID": 2, "FirstName": "Pat", "LastName": "Seller", "Attributes": {}}],
        "contracts": [],
    }
    apply_team_ids(catalog, {}, teams_by_name={"kcbi sales": "t-1"})
    assert catalog["clients"][0]["hubspot_team_id"] == "t-1"
    assert catalog["contacts"][0]["hubspot_team_id"] == "t-1"


def test_collect_includes_product_and_salesrep_keys():
    keys = collect_team_keys(
        {
            "clients": [{"ID": 1, "Name": "Adv", "IsAdvertiser": True, "SalesRepName": "Jane Doe", "Attributes": {}}],
            "contracts": [{"ID": 2, "ProductNames": ["KCBI-AM"], "SalesTeams": ["KCBI Sales"], "Attributes": {}}],
            "reps": [{"id": "7", "name": "Jane Doe", "SalesTeams": ["KCBI Sales"]}],
        }
    )
    by_source = {(row["source"], row["aquira_label"]) for row in keys}
    assert ("product", "KCBI-AM") in by_source
    assert ("salesrep", "Jane Doe") in by_source
    assert ("salesteam", "KCBI Sales") in by_source


def test_normalize_contract_keeps_product_and_sales_team():
    from app.aquira.normalize import normalize_contract

    contract = normalize_contract(
        {
            "ID": 85,
            "ContractCD": "1115",
            "Product": {"Name": "KCBI-AM", "ID": 3},
            "SalesReps": [
                {
                    "Selected": True,
                    "SalesRepID": {"ID": 7, "Name": "Jane Doe", "SalesTeam": {"Name": "KCBI Sales", "ID": 1}},
                    "SalesTeam": {"Name": "KCBI Sales", "ID": 1},
                }
            ],
            "SpotLines": [
                {
                    "StartDate": "2026-01-01",
                    "EndDate": "2026-03-31",
                    "NetAmount": 100,
                    "Products": [{"Name": "KCBI-AM"}],
                    "SelectedStationsCombined": [{"Name": "KCBI"}],
                }
            ],
        }
    )
    assert "KCBI-AM" in contract["ProductNames"]
    assert contract["SalesRepName"] == "Jane Doe"
    assert "KCBI Sales" in contract["SalesTeams"]
    assert contract["lines"][0]["products"] == ["KCBI-AM"]


def test_normalize_client_keeps_team_attribute():
    client = normalize_client(
        {
            "ID": 106,
            "Name": "Client 106",
            "Attributes": [{"Name": "HubSpot Team", "Value": "KCBI Sales"}],
        }
    )
    assert client["Attributes"]["HubSpot Team"] == "KCBI Sales"
    contact = normalize_contact(
        {"ID": 9, "Name": "Pat Seller", "Attributes": [{"Name": "HubSpot Team", "Value": "KLTY Digital"}]},
        106,
    )
    assert contact["Attributes"]["HubSpot Team"] == "KLTY Digital"
    wrapped = normalize_client(
        {
            "ID": 107,
            "Name": "Wrapped",
            "Attributes": [{"Name": {"Value": "HubSpot Team"}, "Value": {"Value": "KCBI Sales"}}],
        }
    )
    assert wrapped["Attributes"]["HubSpot Team"] == "KCBI Sales"
