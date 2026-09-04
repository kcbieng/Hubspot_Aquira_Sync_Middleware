from unittest.mock import MagicMock, patch

from app.aquira.client import AquiraApiError, AquiraSessionClient


def test_login_uses_aquira2go_payload_and_retries_after_loginfailed():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="secret")
    failed = MagicMock(
        status_code=200,
        json=lambda: {"Success": False, "ErrorName": "LoginFailed", "ErrorText": "Already logged in"},
    )
    ok = MagicMock(
        status_code=200,
        json=lambda: {"Success": True, "Entity": {"WebApiVersion": "2024.1"}},
    )
    with patch.object(client.client, "post", side_effect=[failed, ok]) as post_mock:
        with patch.object(client.client, "delete") as delete_mock:
            data = client.login()

    assert data["Success"] is True
    assert client.logged_in is True
    assert client.version == "2024.1"
    first = post_mock.call_args_list[0].kwargs["json"]
    assert first["Username"] == "svc"
    assert first["UserName"] == "svc"
    assert first["IsAquira2GOLogin"] is True
    assert first["WindowsLogin"] is False
    assert delete_mock.called


def test_login_raises_after_retries_exhausted():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="bad")
    failed = MagicMock(
        status_code=200,
        json=lambda: {"Success": False, "ErrorName": "LoginFailed", "ErrorText": "Invalid user"},
    )
    with patch.object(client.client, "post", return_value=failed):
        with patch.object(client.client, "delete"):
            try:
                client.login()
            except AquiraApiError as exc:
                assert "Invalid user" in str(exc)
            else:
                raise AssertionError("expected AquiraApiError")
