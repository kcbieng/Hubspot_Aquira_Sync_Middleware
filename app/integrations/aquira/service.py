from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.integrations.aquira.client import AquiraApiError, validate_response


def _unwrap_aquira_value(value: Any) -> Any:
    if isinstance(value, dict):
        if "Value" in value:
            return _unwrap_aquira_value(value["Value"])
        return {k: _unwrap_aquira_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_aquira_value(item) for item in value]
    return value


class AquiraSessionClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or get_settings().aquira_base_url).rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)

    def login(self) -> None:
        settings = get_settings()
        payload = {"UserName": settings.aquira_username, "Password": settings.aquira_password}
        response = self._client.post("/Session/Post", json=payload)
        data = response.json()
        validate_response(data)

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(method, path, **kwargs)
        if response.status_code == 401:
            self.login()
            response = self._client.request(method, path, **kwargs)
        payload = response.json()
        return validate_response(payload)

    def heartbeat(self) -> bool:
        response = self._client.head("/User/HeartBeat")
        return response.status_code in (200, 204)

    def logout(self) -> None:
        self._client.delete("/Session/Delete")

    def close(self) -> None:
        self._client.close()

    def fetch_client_search(self) -> Any:
        response = self._client.post("/Client/Search", json={"SearchTerm": ""})
        data = response.json()
        return validate_response(data)

    def load_client(self, client_id: str | int) -> dict[str, Any]:
        payload = self.request("POST", f"/Client/Load/{client_id}")
        entity = payload.get("Entity", {})
        return _unwrap_aquira_value(entity)

    def search_clients(self, query: str) -> list[dict[str, Any]]:
        payload = self.request("POST", "/Client/Search", json={"SearchTerm": query})
        rows = payload.get("Data", [])
        normalized_query = (query or "").strip().lower()
        matches: list[dict[str, Any]] = []
        for row in rows:
            candidate = _unwrap_aquira_value(row)
            name = candidate.get("Name") or candidate.get("ShortName") or ""
            raw_id = candidate.get("ID")
            if not normalized_query:
                matches.append(candidate)
                continue

            name_text = str(name).strip()
            name_lower = name_text.lower()
            if name_lower == normalized_query or normalized_query in name_lower or str(raw_id) == str(query):
                matches.append(candidate)
        return matches

    def update_client(self, entity: dict[str, Any], sparse: bool = True, save: bool = True) -> dict[str, Any]:
        payload = {"Save": save, "Sparse": sparse, "Entity": entity}
        return self.request("PUT", "/Client/Put", json=payload)
