from app.mapping.owners import suggest_owner_map


def test_owner_suggest_prefers_email_then_name():
    aquira_reps = [
        {"id": 1, "name": "Jane Smith", "email": "jane@acme.example"},
        {"id": 2, "name": "Bob Jones", "email": "bob@other.example"},
        {"id": 3, "name": "Casey White", "email": ""},
    ]
    hubspot_owners = [
        {"owner_id": "hs-1", "name": "Jane Smith", "email": "jane@acme.example"},
        {"owner_id": "hs-2", "name": "Bobby Jones", "email": "bobby@other.example"},
        {"owner_id": "hs-3", "name": "Casey White", "email": "casey@fresh.example"},
    ]

    suggestions = suggest_owner_map(aquira_reps, hubspot_owners)
    assert suggestions[0]["hubspot_owner_id"] == "hs-1"
    assert suggestions[1]["hubspot_owner_id"] == "hs-2"
    assert suggestions[2]["hubspot_owner_id"] == "hs-3"
    assert suggestions[0]["suggested"] is True
