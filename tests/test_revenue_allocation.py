import json
from pathlib import Path

from app.mapping.revenue import allocate_revenue


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
