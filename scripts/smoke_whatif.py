#!/usr/bin/env python3
"""Optional smoke test: check that live Aquira credentials can reach Search endpoints."""

from __future__ import annotations

import os
import sys

from app.aquira.client import AquiraSessionClient
from app.settings import get_settings


def main() -> int:
    settings = get_settings()
    if not settings.aquira_username or not settings.aquira_password:
        print("Aquira credentials not configured; skipping live smoke test.")
        return 0

    client = AquiraSessionClient(settings.aquira_base_url)
    try:
        client.login()
        search = client.client.post("/Client/Search", json={"SearchTerm": ""})
        print(f"client_search_status={search.status_code}")
        contract = client.client.post("/Contract/Search", json={"SearchTerm": ""})
        print(f"contract_search_status={contract.status_code}")
        return 0
    finally:
        try:
            client.logout()
        except Exception:
            pass
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
