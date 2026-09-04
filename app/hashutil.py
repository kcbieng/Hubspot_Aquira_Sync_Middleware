from __future__ import annotations

import hashlib
import json
from typing import Any


def _sort_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_sort_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sort_value(value[key]) for key in sorted(value)}
    return value


def content_hash(value: Any) -> str:
    payload = json.dumps(_sort_value(value), separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
