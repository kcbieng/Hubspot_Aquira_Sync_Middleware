from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings
from app.webhooks.aquira import extract_notification, parse_body


client = TestClient(app)


def test_extract_json_contract_id():
    extracted = extract_notification({"ContractID": 49, "Name": "Proposal submitted"})
    assert extracted["ids"] == ["49"]
    assert "Proposal submitted" in extracted["event"]


def test_extract_fieldvalue_and_text():
    extracted = extract_notification(
        {"Entity": {"ID": {"Value": 85}, "ContractCD": {"Value": "1115"}}},
        "Contract 85 was modified",
    )
    assert "85" in extracted["ids"]
    assert "1115" in extracted["contract_cds"]


def test_extract_xml_contract():
    parsed = parse_body(b"<Notification><ContractID>49</ContractID><Event>accepted</Event></Notification>", "application/xml")
    extracted = extract_notification(parsed)
    assert "49" in extracted["ids"]


def test_aquira_webhook_get_probe():
    response = client.get("/webhooks/aquira")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_aquira_webhook_requires_token_when_configured():
    settings = get_settings()
    original = settings.aquira_webhook_secret
    settings.aquira_webhook_secret = "s3cret"
    try:
        denied = client.post("/webhooks/aquira", json={"ContractID": 1})
        assert denied.status_code == 401
        with patch("app.webhooks.routes.kick_aquira_sync") as kick:
            accepted = client.post(
                "/webhooks/aquira?token=s3cret",
                json={"ContractID": 9404, "Event": "spotlines modified", "nonce": "token-check"},
            )
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["status"] in {"accepted", "duplicate"}
        assert "9404" in body["extracted"]["ids"]
        if body["status"] == "accepted":
            assert kick.called
    finally:
        settings.aquira_webhook_secret = original
