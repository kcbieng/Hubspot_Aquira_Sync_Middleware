from unittest.mock import MagicMock, patch

from app.aquira.client import AquiraApiError, AquiraSessionClient


def test_login_sends_swagger_username_password_without_2go_flag():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="secret")
    ok = MagicMock(
        status_code=200,
        json=lambda: {"Success": True, "Entity": {"WebApiVersion": "2024.1"}},
    )
    with patch.object(client.client, "post", return_value=ok) as post_mock:
        data = client.login()

    assert data["Success"] is True
    payload = post_mock.call_args.kwargs["json"]
    assert payload == {"Username": "svc", "Password": "secret"}
    assert "IsAquira2GOLogin" not in payload
    assert "WindowsLogin" not in payload


def test_login_retries_username_variant_after_loginfailed():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="secret")
    failed = MagicMock(
        status_code=200,
        json=lambda: {"Success": False, "ErrorName": "LoginFailed", "ErrorText": "Already logged in", "Error": 1},
    )
    ok = MagicMock(
        status_code=200,
        json=lambda: {"Success": True, "Entity": {"WebApiVersion": "2024.1"}},
    )
    with patch.object(client.client, "post", side_effect=[failed, ok]) as post_mock:
        with patch.object(client.client, "delete"):
            data = client.login()

    assert data["Success"] is True
    assert post_mock.call_args_list[1].kwargs["json"] == {"UserName": "svc", "Password": "secret"}


def test_login_rejects_fernet_blob_as_password():
    client = AquiraSessionClient(
        base_url="https://example.test",
        username="svc",
        password="gAAAAABnotarealpasswordblob",
    )
    try:
        client.login()
    except AquiraApiError as exc:
        assert "decrypt" in str(exc).lower()
    else:
        raise AssertionError("expected AquiraApiError")


def test_login_raises_after_retries_exhausted():
    client = AquiraSessionClient(base_url="https://example.test", username="svc", password="bad")
    failed = MagicMock(
        status_code=200,
        json=lambda: {"Success": False, "ErrorName": "LoginFailed", "ErrorText": "Invalid user", "Error": 1},
    )
    with patch.object(client.client, "post", return_value=failed):
        with patch.object(client.client, "delete"):
            try:
                client.login()
            except AquiraApiError as exc:
                assert "Invalid user" in str(exc)
                assert "LoginFailed" in str(exc)
            else:
                raise AssertionError("expected AquiraApiError")
