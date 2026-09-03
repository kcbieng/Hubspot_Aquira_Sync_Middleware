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

    spec_payload = b"".join([method, uri, payload, timestamp.encode("utf-8")])
    legacy_payload = payload + timestamp.encode("utf-8")
    expected_spec = base64.b64encode(hmac.new(client_secret.encode("utf-8"), spec_payload, hashlib.sha256).digest()).decode("utf-8")
    expected_legacy = base64.b64encode(hmac.new(client_secret.encode("utf-8"), legacy_payload, hashlib.sha256).digest()).decode("utf-8")
    return hmac.compare_digest(expected_spec, signature) or hmac.compare_digest(expected_legacy, signature)


def webhook_event_is_duplicate(message_id: str, seen_ids: set[str]) -> bool:
    return message_id in seen_ids
