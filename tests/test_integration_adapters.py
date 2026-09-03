from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.aquira.client import AquiraSessionClient
from app.hubspot.client import HubSpotClient


def test_aquira_retries_once_after_401():
    client = AquiraSessionClient(base_url="https://example.test")
    responses = [
        MagicMock(status_code=401, json=lambda: {"Success": False, "Error": 401}),
        MagicMock(status_code=200, json=lambda: {"Success": True, "Data": [{"ID": 42}]}),
    ]

    with patch.object(client.client, "request", side_effect=[responses[0], responses[1]]) as request_mock:
        with patch.object(client, "login", return_value={"Success": True}) as login_mock:
            result = client.request("GET", "/Client/Load/42")

    assert result == {"Success": True, "Data": [{"ID": 42}]}
    assert login_mock.call_count == 1
    assert request_mock.call_count == 2


def test_hubspot_owner_map_uses_email_lookup():
    client = HubSpotClient(access_token="token")
    payload = {
        "results": [
            {"ownerId": "hs-1", "email": "jane@example.com", "firstName": "Jane"},
            {"ownerId": "hs-2", "email": "bob@example.com", "firstName": "Bob"},
        ]
    }

    with patch.object(client, "get_owners", return_value=payload):
        owner_map = client.get_owner_map()

    assert owner_map["jane@example.com"] == "hs-1"
    assert owner_map["bob@example.com"] == "hs-2"
