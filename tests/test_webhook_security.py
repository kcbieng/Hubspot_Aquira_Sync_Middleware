import base64
import hashlib
import hmac
import time

from app.webhooks.hubspot import verify_hubspot_signature


def test_verify_hubspot_signature_uses_payload_and_timestamp():
    secret = "abc123"
    method = "POST"
    uri = "/webhooks/hubspot"
    body = '{"hello":"world"}'
    timestamp = str(int(time.time()))
    signature = base64.b64encode(
        hmac.new(secret.encode(), f"{method}{uri}{body}{timestamp}".encode(), hashlib.sha256).digest()
    ).decode()

    assert verify_hubspot_signature(method.encode(), uri.encode(), body.encode(), timestamp, signature, secret) is True


def test_verify_hubspot_signature_rejects_stale_request():
    secret = "abc123"
    timestamp = str(int(time.time()) - 301)
    assert verify_hubspot_signature(b"POST", b"/webhooks/hubspot", b'{"hello":"world"}', timestamp, "bad", secret) is False
