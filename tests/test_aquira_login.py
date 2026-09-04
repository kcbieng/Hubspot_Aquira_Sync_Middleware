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


def test_login_raises_loginfailed_without_retry():
    client = AquiraSessionClient(base_url="https://example.test", username="hubquira", password="bad")
    failed = MagicMock(
        status_code=200,
        json=lambda: {"Success": False, "ErrorName": "LoginFailed", "Error": -7},
    )
    with patch.object(client.client, "post", return_value=failed) as post_mock:
        try:
            client.login()
        except AquiraApiError as exc:
            assert "LoginFailed" in str(exc)
            assert "code=-7" in str(exc)
        else:
            raise AssertionError("expected AquiraApiError")
    assert post_mock.call_count == 1
