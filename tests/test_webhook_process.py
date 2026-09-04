from app.webhooks.hubspot import identity_targets, normalize_events, process_hubspot_identity_events


def test_normalize_events_accepts_hubspot_array():
    message_id, events = normalize_events(
        [
            {"eventId": 99, "subscriptionType": "company.propertyChange", "objectId": 55, "propertyName": "name"},
        ]
    )
    assert message_id == "99"
    assert events[0]["objectId"] == 55


def test_identity_targets_only_client_contact_fields():
    companies, contacts, create_missing = identity_targets(
        [
            {"subscriptionType": "company.propertyChange", "objectId": "1", "propertyName": "name"},
            {"subscriptionType": "company.propertyChange", "objectId": "2", "propertyName": "aquira_id"},
            {"subscriptionType": "contact.propertyChange", "objectId": "3", "propertyName": "email"},
            {"subscriptionType": "deal.propertyChange", "objectId": "4", "propertyName": "amount"},
            {"subscriptionType": "company.creation", "objectId": "5"},
        ]
    )
    assert companies == {"1", "5"}
    assert contacts == {"3"}
    assert create_missing is True


def test_process_skips_when_no_identity_events():
    result = process_hubspot_identity_events([{"messageId": "evt-dup"}])
    assert result["processed"] == 0
