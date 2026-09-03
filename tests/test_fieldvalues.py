import json
from pathlib import Path

from app.aquira.fieldvalues import sparse_put, unwrap

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str):
    return json.loads((FIXTURE_DIR / name).read_text())


def test_unwrap_fieldvalue_and_sparse_put():
    fixture = _load_fixture("fieldvalue_client.json")["Entity"]
    assert unwrap(fixture["ShortName"]) == "ACME"
    assert unwrap(fixture["PhysicalAddress"]) == "100 Main St"

    sparse = sparse_put(
        fixture,
        ["ShortName", "Email", "Phone", "Website", "PhysicalAddress", "IsAccount"],
    )
    assert set(sparse) == {"ShortName", "Email", "Phone", "Website", "PhysicalAddress"}
    assert sparse["Email"] == "billing@acme.example"
    assert "IsAccount" not in sparse


def test_fieldvalue_advertiser_and_both_party_flags():
    advertiser = _load_fixture("fieldvalue_advertiser.json")["Entity"]
    both = _load_fixture("fieldvalue_both.json")["Entity"]
    assert unwrap(advertiser["IsAdvertiser"]) is True
    assert unwrap(both["IsAccount"]) is True
    assert unwrap(both["IsAdvertiser"]) is True
