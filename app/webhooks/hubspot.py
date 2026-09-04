from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any


def verify_hubspot_signature(method: bytes, uri: bytes, payload: bytes, timestamp: str, signature: str, client_secret: str) -> bool:
    if not timestamp or not signature or not client_secret:
        return False
    try:
        ts_value = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts_value) > 300:
        return False

    method_bytes = method if isinstance(method, bytes) else str(method).encode("utf-8")
    uri_bytes = uri if isinstance(uri, bytes) else str(uri).encode("utf-8")
    timestamp_bytes = timestamp.encode("utf-8")

    spec_payload = b"".join([method_bytes, uri_bytes, payload, timestamp_bytes])
    legacy_payload = payload + timestamp_bytes
    body_plus_ts = timestamp_bytes + payload

    expected_spec = base64.b64encode(hmac.new(client_secret.encode("utf-8"), spec_payload, hashlib.sha256).digest()).decode("utf-8")
    expected_legacy = base64.b64encode(hmac.new(client_secret.encode("utf-8"), legacy_payload, hashlib.sha256).digest()).decode("utf-8")
    expected_body_ts = base64.b64encode(hmac.new(client_secret.encode("utf-8"), body_plus_ts, hashlib.sha256).digest()).decode("utf-8")
    return hmac.compare_digest(expected_spec, signature) or hmac.compare_digest(expected_legacy, signature) or hmac.compare_digest(expected_body_ts, signature)


def webhook_event_is_duplicate(message_id: str, seen_ids: set[str]) -> bool:
    return message_id in seen_ids
