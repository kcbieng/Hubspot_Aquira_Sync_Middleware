#!/usr/bin/env python3
"""Live connectivity check. Reads current env/settings. Never writes."""

from __future__ import annotations

import json
import sys


def main() -> int:
    from app.aquira.client import test_aquira_connection
    from app.hubspot.client import HubSpotClient
    from app.settings import get_settings

    settings = get_settings()
    aquira = test_aquira_connection(settings)
    hubspot = HubSpotClient().test_connection() if settings.hubspot_access_token else {
        "status": "error",
        "mode": "live",
        "message": "HubSpot token missing",
        "portal": "unconfigured",
    }
    payload = {
        "whatif": settings.whatif,
        "aquira": aquira,
        "hubspot": hubspot,
        "ok": aquira.get("status") == "ok" and hubspot.get("status") == "ok",
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
