from app.aquira.contracts import normalize_spotlines


def test_normalize_spotlines_unwraps_field_value_wrappers():
    raw_load = {
        "Entity": {
            "ID": 9001,
            "ContractCD": {"Value": "C-9001"},
            "IsContract": True,
            "StartDate": {"Value": "2026-01-15"},
            "EndDate": {"Value": "2026-03-15"},
            "TotalValue": {"Value": 12000},
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

    normalized = normalize_spotlines(raw_load, raw_detail)

    assert normalized["contract_id"] == 9001
    assert normalized["contract_cd"] == "C-9001"
    assert normalized["kind"] == "booked"
    assert normalized["lines"][0]["station_id"] == 10
    assert normalized["lines"][0]["amount"] == 12000
    assert normalized["lines"][0]["start"] == "2026-01-15"
    assert normalized["lines"][0]["end"] == "2026-03-15"
