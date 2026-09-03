from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.integrations.aquira.service import AquiraSessionClient


def test_aquira_integration_service_retries_after_401():
    client = AquiraSessionClient(base_url="https://example.test")
    responses = [
        MagicMock(status_code=401, json=lambda: {"Success": False, "Error": 401}),
        MagicMock(status_code=200, json=lambda: {"Success": True, "Data": [{"ID": 42}]}),
    ]

    with patch.object(client._client, "request", side_effect=[responses[0], responses[1]]) as request_mock:
        with patch.object(client, "login", return_value=None) as login_mock:
            result = client.request("GET", "/Client/Load/42")

    assert result == {"Success": True, "Data": [{"ID": 42}]}
    assert login_mock.call_count == 1
    assert request_mock.call_count == 2


def test_aquira_integration_service_update_client_uses_sparse_payload():
    client = AquiraSessionClient(base_url="https://example.test")
    entity = {"ID": 7, "ShortName": {"Value": "Acme", "Access": 2}, "IsAccount": {"Value": True, "Access": 1}}

    with patch.object(client, "request", return_value={"Success": True, "Entity": entity}) as request_mock:
        result = client.update_client(entity, sparse=True)

    assert result["Success"] is True
    assert request_mock.call_count == 1
    request_payload = request_mock.call_args.kwargs["json"]
    assert request_payload["Sparse"] is True
    assert request_payload["Save"] is True
    assert request_payload["Entity"]["ID"] == 7


def test_aquira_integration_service_load_client_unwraps_field_values():
    client = AquiraSessionClient(base_url="https://example.test")
    payload = {
        "Success": True,
        "Entity": {
            "ID": 7,
            "Name": {"Value": "Acme Media", "Access": 1},
            "IsAccount": {"Value": True, "Access": 0},
            "IsAdvertiser": {"Value": False, "Access": 0},
        },
    }

    with patch.object(client, "request", return_value=payload) as request_mock:
        result = client.load_client(7)

    assert result["ID"] == 7
    assert result["Name"] == "Acme Media"
    assert result["IsAccount"] is True
    assert result["IsAdvertiser"] is False
    assert request_mock.call_count == 1


def test_aquira_integration_service_search_filters_exact_name_matches():
    client = AquiraSessionClient(base_url="https://example.test")
    payload = {
        "Success": True,
        "Data": [
            {"ID": 1, "Name": {"Value": "Acme Holdings"}},
            {"ID": 2, "Name": {"Value": "Acme Media"}},
            {"ID": 3, "Name": {"Value": "Acme"}},
        ],
    }

    with patch.object(client, "request", return_value=payload):
        result = client.search_clients("Acme Media")

    assert len(result) == 1
    assert result[0]["ID"] == 2
    assert result[0]["Name"] == "Acme Media"


def test_aquira_integration_service_search_accepts_contains_query_results():
    client = AquiraSessionClient(base_url="https://example.test")
    payload = {
        "Success": True,
        "Data": [
            {"ID": 1, "Name": {"Value": "Acme Holdings"}},
            {"ID": 2, "Name": {"Value": "Acme Media"}},
            {"ID": 3, "Name": {"Value": "Globex"}},
        ],
    }

    with patch.object(client, "request", return_value=payload):
        result = client.search_clients("Acme")

    assert [row["ID"] for row in result] == [1, 2]
