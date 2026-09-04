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
