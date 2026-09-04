from unittest.mock import patch

from app.aquira.client import AquiraSessionClient
from app.aquira.normalize import normalize_contact


def test_lookup_contacts_sends_entity_load_id():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="x")
    payload = {
        "Success": True,
        "Data": [
            {
                "ID": 9,
                "Name": "Pat Seller",
                "EmailAddress": "pat@example.com",
                "BusinessPhone1": "2145550100",
                "Client": {"ID": 106, "Name": "Client 106"},
            }
        ],
    }
    with patch.object(client, "request", return_value=payload) as request_mock:
        rows = client.lookup_contacts(106)

    assert request_mock.call_args.args[:2] == ("POST", "/Client/LookupContacts")
    assert request_mock.call_args.kwargs["json"] == {"id": 106, "name": "lookup-contacts"}
    assert rows[0]["ID"] == 9
    assert rows[0]["ClientID"] == 106
    assert rows[0]["Email"] == "pat@example.com"
    assert rows[0]["Phone"] == "2145550100"
    assert rows[0]["FirstName"] == "Pat"
    assert rows[0]["LastName"] == "Seller"


def test_lookup_contacts_skips_invalid_id():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="x")
    with patch.object(client, "try_request") as request_mock:
        assert client.lookup_contacts("") == []
        assert client.lookup_contacts(0) == []
    request_mock.assert_not_called()


def test_normalize_contact_uses_email_address_and_nested_client():
    contact = normalize_contact(
        {
            "ID": 12,
            "Name": "Alex Rep",
            "EmailAddress": "alex@kcbi.org",
            "PersonalMobilePhone": "2145550199",
            "Client": {"ID": 44},
        }
    )
    assert contact["Email"] == "alex@kcbi.org"
    assert contact["Phone"] == "2145550199"
    assert contact["ClientID"] == 44
