import json
from pathlib import Path

from app.aquira.normalize import normalize_charge_lines, normalize_contract, normalize_revenue_months
from app.mapping.revenue import allocate_revenue, summarize_allocation
from app.sync.planner import deal_properties, plan_revenue


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str):
    return json.loads((FIXTURE_DIR / name).read_text())


def test_even_split_example_a():
    fixture = _load_fixture("spotlines_even.json")
    periods = allocate_revenue(fixture)
    assert [(p["aquira_id"], p["amount"]) for p in periods] == [
        ("9001:2026-01:10", 4000.0),
        ("9001:2026-02:10", 4000.0),
        ("9001:2026-03:10", 4000.0),
    ]


def test_weighted_split_example_b():
    fixture = _load_fixture("spotlines_weighted.json")
    periods = allocate_revenue(fixture)
    assert [(p["aquira_id"], p["amount"]) for p in periods] == [
        ("9002:2026-01:10", 750.0),
        ("9002:2026-02:10", 250.0),
    ]


def test_missing_lines_fallback_example_c():
    fixture = _load_fixture("spotlines_missing.json")
    periods = allocate_revenue(fixture)
    assert [(p["aquira_id"], p["amount"]) for p in periods] == [
        ("9003:2026-01:0", 4000.0),
        ("9003:2026-02:0", 4000.0),
        ("9003:2026-03:0", 4000.0),
    ]
    summary = summarize_allocation(
        {"TotalValue": 12000, "lines": []},
        periods,
    )
    assert summary["mismatch"] is False
    assert summary["allocated_total"] == 12000.0


def test_charge_lines_land_in_their_month():
    periods = allocate_revenue(
        {
            "contract_id": 49,
            "contract_cd": "1070",
            "kind": "booked",
            "fallback_start": "2026-01-01",
            "fallback_end": "2026-03-31",
            "fallback_amount": 13000,
            "lines": [
                {
                    "station_id": 10,
                    "station": "KCBI",
                    "start": "2026-01-01",
                    "end": "2026-03-31",
                    "amount": 12000,
                    "line_kind": "spot",
                    "spots_by_month": {},
                    "seconds_by_month": {},
                },
                {
                    "station_id": 10,
                    "station": "KCBI",
                    "start": "2026-01-15",
                    "end": "2026-01-15",
                    "amount": 1000,
                    "line_kind": "charge",
                },
            ],
        }
    )
    by_id = {row["aquira_id"]: row for row in periods}
    assert by_id["49:2026-01:10"]["amount"] == 5000.0
    assert by_id["49:2026-01:10"]["charge_amount"] == 1000.0
    assert by_id["49:2026-01:10"]["spot_amount"] == 4000.0
    assert by_id["49:2026-02:10"]["amount"] == 4000.0
    assert by_id["49:2026-03:10"]["amount"] == 4000.0
    assert sum(row["amount"] for row in periods) == 13000.0


def test_sanity_check_flags_when_aquira_total_does_not_match_lines():
    contract = {
        "ID": 49,
        "ContractCD": "1070",
        "TotalValue": 15000,
        "IsContract": True,
        "StartDate": "2026-01-01",
        "EndDate": "2026-01-31",
        "lines": [
            {"station_id": 10, "station": "KCBI", "start": "2026-01-01", "end": "2026-01-31", "amount": 12000, "line_kind": "spot"},
            {"station_id": 10, "station": "KCBI", "start": "2026-01-10", "end": "2026-01-10", "amount": 1000, "line_kind": "charge"},
        ],
    }
    props = deal_properties(contract)
    assert props["amount"] == 15000
    assert props["aquira_allocated_amount"] == 13000.0
    assert props["aquira_spot_total"] == 12000.0
    assert props["aquira_charge_total"] == 1000.0
    assert props["aquira_amount_mismatch"] is True
    assert props["aquira_amount_delta"] == 2000.0


def test_sanity_check_passes_when_total_equals_spots_plus_charges():
    contract = {
        "ID": 49,
        "ContractCD": "1070",
        "TotalValue": 13000,
        "IsContract": True,
        "StartDate": "2026-01-01",
        "EndDate": "2026-01-31",
        "AccountID": 101,
        "AdvertiserID": 101,
        "lines": [
            {"station_id": 10, "station": "KCBI", "start": "2026-01-01", "end": "2026-01-31", "amount": 12000, "line_kind": "spot"},
            {"station_id": 10, "station": "KCBI", "start": "2026-01-10", "end": "2026-01-10", "amount": 1000, "line_kind": "charge"},
        ],
    }
    props = deal_properties(contract)
    assert props["aquira_amount_mismatch"] is False
    items = plan_revenue([contract], {})
    assert items[0]["associations"]["dealId"] == "49"
    assert items[0]["properties"]["deal_aquira_id"] == "49"
    assert items[0]["properties"]["amount"] == 13000.0


def test_normalize_charge_lines_unwraps_field_values():
    payload = {
        "Entity": {
            "ID": 49,
            "ChargeLines": {
                "Value": [
                    {
                        "Value": {
                            "GrossAmount": {"Value": 500},
                            "Date": {"Value": "2026-02-01T00:00:00"},
                            "ChargeType": {"Value": {"Name": "Production"}},
                            "SelectedStationsCombined": {"Value": [{"ID": 10, "Name": "KCBI"}]},
                        }
                    }
                ]
            },
        }
    }
    charges = normalize_charge_lines(payload)
    assert len(charges) == 1
    assert charges[0]["amount"] == 500
    assert charges[0]["start"] == "2026-02-01"
    assert charges[0]["line_kind"] == "charge"
    contract = normalize_contract(
        {
            "Entity": {
                "ID": 49,
                "ContractCD": {"Value": "1070"},
                "IsContract": True,
                "TotalValue": {"Value": 500},
                "ChargeLines": payload["Entity"]["ChargeLines"],
            }
        }
    )
    assert contract["lines"][0]["line_kind"] == "charge"
    assert contract["lines"][0]["amount"] == 500


def test_normalize_revenue_months_splits_spot_and_charge():
    lines = normalize_revenue_months(
        {
            "Success": True,
            "Data": {
                "Items": [
                    {
                        "StationShortName": "KCBI",
                        "Year": 2026,
                        "Month": 1,
                        "SpotGrossAmount": 4000,
                        "ChargeGrossAmount": 500,
                        "SponsorshipGrossAmount": 0,
                        "WebGrossAmount": 0,
                        "NetAmount": 4500,
                    },
                    {
                        "StationShortName": "KCBI",
                        "Year": 2026,
                        "Month": 2,
                        "SpotGrossAmount": 2500,
                        "ChargeGrossAmount": 0,
                        "NetAmount": 2500,
                    },
                ]
            },
        }
    )
    assert [(row["line_kind"], row["start"], row["amount"]) for row in lines] == [
        ("spot", "2026-01-01", 4000.0),
        ("charge", "2026-01-01", 500.0),
        ("spot", "2026-02-01", 2500.0),
    ]
    periods = allocate_revenue(
        {
            "contract_id": 257,
            "contract_cd": "1115",
            "kind": "booked",
            "lines": lines,
        }
    )
    by_month = {row["period"]: row for row in periods}
    assert by_month["2026-01-01"]["amount"] == 4500.0
    assert by_month["2026-01-01"]["charge_amount"] == 500.0
    summary = summarize_allocation({"TotalValue": 7000, "lines": lines}, periods)
    assert summary["mismatch"] is False
    assert summary["allocated_total"] == 7000.0


def test_booked_analysis_scales_down_to_contract_total_after_missed_spots():
    lines = normalize_revenue_months(
        {
            "Success": True,
            "Data": {
                "Items": [
                    {
                        "StationShortName": "KCBI-FM",
                        "Year": 2026,
                        "Month": 1,
                        "SpotGrossAmount": 174081.78,
                        "ChargeGrossAmount": 0,
                        "NetAmount": 174081.78,
                    },
                    {
                        "StationShortName": "KCBI-FM",
                        "Year": 2026,
                        "Month": 2,
                        "SpotGrossAmount": 174081.78,
                        "ChargeGrossAmount": 0,
                        "NetAmount": 174081.78,
                    },
                ]
            },
        }
    )
    assert abs(sum(row["amount"] for row in lines) - 348163.56) < 0.01
    periods = allocate_revenue(
        {
            "contract_id": 56,
            "contract_cd": "1078",
            "kind": "booked",
            "fallback_amount": 342179.64,
            "lines": lines,
        }
    )
    allocated = round(sum(row["amount"] for row in periods), 2)
    assert allocated == 342179.64
    summary = summarize_allocation({"TotalValue": 342179.64, "lines": lines}, periods)
    assert summary["mismatch"] is False
    from app.sync.planner import deal_properties

    props = deal_properties(
        {
            "ID": 56,
            "ContractCD": "1078",
            "Description": "Insight for Living",
            "TotalValue": 342179.64,
            "IsContract": True,
            "StartDate": "2026-01-01",
            "EndDate": "2027-11-30",
            "lines": lines,
        }
    )
    assert props["amount"] == 342179.64
    assert props["aquira_allocated_amount"] == 342179.64
    assert props["aquira_booked_amount"] == 348163.56
    assert props["aquira_amount_mismatch"] is False


def test_spot_line_prefers_total_amount_over_booked():
    from app.aquira.normalize import normalize_spot_lines

    lines = normalize_spot_lines(
        {
            "Entity": {
                "SpotLines": [
                    {
                        "StartDate": "2026-01-01",
                        "EndDate": "2026-01-31",
                        "BookedTotalAmount": 1000,
                        "TotalAmount": 800,
                        "SelectedStationsCombined": [{"ID": 10, "Name": "KCBI"}],
                    }
                ]
            }
        }
    )
    assert lines[0]["amount"] == 800
    assert lines[0]["booked_amount"] == 1000
