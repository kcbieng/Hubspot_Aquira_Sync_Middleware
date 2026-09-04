from app.hashutil import content_hash
from app.sync.planner import plan_companies, plan_contacts, plan_deals, plan_identity_writebacks, plan_revenue


def test_plan_companies_skips_when_hash_matches():
    client = {
        "ID": 101,
        "Name": "Sewell Cadillac",
        "Phone": "2145550000",
        "Website": "sewell.com",
        "PhysicalAddress": "Dallas",
        "City": "Dallas",
        "State": "TX",
        "IsAccount": True,
        "IsAdvertiser": True,
    }
    from app.sync.planner import company_properties

    props = company_properties(client)
    existing = {"101": {"hubspotId": "cmp-1", "properties": props, "hash": content_hash(props)}}
    items = plan_companies([client], existing)
    assert items[0]["action"] == "skip"


def test_plan_companies_keeps_hubspot_identity_fields():
    client = {
        "ID": 101,
        "Name": "Sewell Cadillac",
        "Phone": "2145550000",
        "Website": "sewell.com",
        "PhysicalAddress": "Old",
        "City": "Dallas",
        "State": "TX",
        "IsAccount": True,
        "IsAdvertiser": False,
    }
    existing = {
        "101": {
            "hubspotId": "cmp-1",
            "properties": {"name": "Sewell Cadillac of Dallas", "phone": "2145551111", "domain": "sewell.com", "address": "Lemmon Ave", "city": "Dallas", "state": "TX"},
            "hash": "stale",
        }
    }
    items = plan_companies([client], existing)
    assert items[0]["action"] == "update"
    assert items[0]["properties"]["name"] == "Sewell Cadillac of Dallas"
    assert items[0]["properties"]["phone"] == "2145551111"


def test_plan_deals_uses_advertiser_name():
    contract = {
        "ID": 9001,
        "ContractCD": "C-9001",
        "Name": "Spring Flight",
        "IsProposal": False,
        "IsContract": True,
        "TotalValue": 12000,
        "StartDate": "2026-01-01",
        "EndDate": "2026-03-31",
        "AccountID": 101,
        "AdvertiserID": 202,
        "SalesRepID": 7,
        "Cancelled": False,
        "lines": [],
    }
    items = plan_deals([contract], {}, {"7": "hs-owner"}, {"202": "Park Cities Baptist"})
    assert items[0]["properties"]["dealname"] == "C-9001 — Park Cities Baptist"
    assert items[0]["properties"]["hubspot_owner_id"] == "hs-owner"
    assert items[0]["properties"]["dealstage"] == "closedwon"


def test_plan_deals_prefers_contract_description_for_name():
    contract = {
        "ID": 49,
        "ContractCD": "1070",
        "Name": "Park Cities Baptist",
        "Description": "Christmas 2025 — morning drive",
        "IsProposal": False,
        "IsContract": True,
        "TotalValue": 12500,
        "StartDate": "2026-01-01",
        "EndDate": "2026-03-31",
        "AccountID": 101,
        "AdvertiserID": 202,
        "SalesRepID": 7,
        "Cancelled": False,
        "lines": [],
    }
    items = plan_deals([contract], {}, {}, {"202": "Park Cities Baptist"})
    assert items[0]["properties"]["dealname"] == "1070 — Christmas 2025 — morning drive"


def test_plan_revenue_emits_monthly_periods_and_stale_deletes():
    contract = {
        "ID": 1,
        "ContractCD": "C-1",
        "IsContract": True,
        "TotalValue": 3000,
        "StartDate": "2026-01-15",
        "EndDate": "2026-03-15",
        "AccountID": 10,
        "AdvertiserID": 10,
        "lines": [],
    }
    existing = {"1:2026-04:0": {"hubspotId": "rev-old", "properties": {"amount": 1}}}
    items = plan_revenue([contract], existing)
    actions = {item["action"] for item in items}
    assert "create" in actions
    assert "delete-stale" in actions


def test_plan_identity_writeback_when_hubspot_is_sot():
    items = plan_identity_writebacks(
        [{"aquira_id": "101", "hubspotId": "cmp-1", "name": "Sewell", "properties": {"name": "Sewell Cadillac", "phone": "2145559999", "domain": "sewell.com", "address": "Dallas"}}],
        {"101": {"ID": 101, "Name": "Sewell Cadillac", "Phone": "2145550000", "Website": "sewell.com", "PhysicalAddress": "Dallas"}},
    )
    assert items[0]["writeback"] is True
    assert items[0]["properties"]["Phone"] == "2145559999"
