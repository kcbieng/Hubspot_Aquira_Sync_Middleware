import base64
import hashlib
import hmac
import time

from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings


def _make_signature(secret: str, body: str, timestamp: str) -> str:
    digest = hmac.new(secret.encode(), body.encode() + timestamp.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_webhook_rejects_missing_or_invalid_signature():
    settings = get_settings()
    settings.hubspot_client_secret = "test-secret"
    client = TestClient(app)
    body = '{"messageId":"evt-1"}'
    response = client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-HubSpot-Request-Timestamp": str(int(time.time())),
            "X-HubSpot-Signature-v3": "bad",
        },
    )
    assert response.status_code == 401


def test_webhook_accepts_valid_signature_and_dedupe():
    settings = get_settings()
    settings.hubspot_client_secret = "test-secret"
    client = TestClient(app)
    body = '{"messageId":"evt-dup"}'
    timestamp = str(int(time.time()))
    signature = _make_signature("test-secret", body, timestamp)
    response = client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-HubSpot-Request-Timestamp": timestamp,
            "X-HubSpot-Signature-v3": signature,
        },
    )
    assert response.status_code == 200
    second = client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-HubSpot-Request-Timestamp": timestamp,
            "X-HubSpot-Signature-v3": signature,
        },
    )
    assert second.status_code == 200
