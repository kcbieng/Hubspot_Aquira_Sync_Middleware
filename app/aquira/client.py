from __future__ import annotations

from typing import Any

import httpx

from app.settings import get_settings


def _unwrap_aquira_value(value: Any) -> Any:
    if isinstance(value, dict):
        if "Value" in value:
            return _unwrap_aquira_value(value["Value"])
        return {k: _unwrap_aquira_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_aquira_value(item) for item in value]
    return value


class AquiraApiError(RuntimeError):
    pass


def unwrap_field_value(value: Any) -> Any:
    if isinstance(value, dict) and "Value" in value:
        return value.get("Value")
    return value


def validate_response(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("Success") is True:
        return payload
    error = payload.get("Error")
    errors = payload.get("Errors")
    raise AquiraApiError(f"Aquira request failed: Error={error}; Errors={errors}; ErrorName={payload.get('ErrorName')}")


class AquiraSessionClient:
    def __init__(self, base_url: str | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.aquira_base_url).rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0)

    def login(self) -> dict[str, Any]:
        settings = get_settings()
        payload = {"UserName": settings.aquira_username, "Password": settings.aquira_password}
        response = self.client.post("/Session/Post", json=payload)
        data = response.json()
        return validate_response(data)

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.client.request(method, path, **kwargs)
        if response.status_code == 401:
            self.login()
            response = self.client.request(method, path, **kwargs)
        payload = response.json()
        return validate_response(payload)

    def heartbeat(self) -> bool:
        response = self.client.head("/User/HeartBeat")
        return response.status_code in (200, 204)

    def search_clients(self, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self.request("POST", "/Client/Search", json=payload or {})
        rows = result.get("Data") or result.get("Entity") or []
        if isinstance(rows, dict):
            rows = [rows]
        return [_unwrap_aquira_value(row) for row in rows if row]

    def lookup_client(self, lookup: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self.request("POST", "/Client/Lookup", json=lookup or {})
        rows = result.get("Data") or result.get("Entity") or []
        if isinstance(rows, dict):
            rows = [rows]
        return [_unwrap_aquira_value(row) for row in rows if row]

    def load_client(self, client_id: str | int) -> dict[str, Any]:
        payload = self.request("POST", f"/Client/Load/{client_id}")
        entity = payload.get("Entity", {})
        return _unwrap_aquira_value(entity)

    def create_client(self, entity: dict[str, Any], *, whatif: bool = False) -> dict[str, Any]:
        if whatif:
            return {"Success": True, "whatif": True, "Entity": entity}
        result = self.request("POST", "/Client/Create", json={"Entity": entity, "Save": True})
        return result

    def update_client(self, client_id: str | int, entity: dict[str, Any], *, sparse: bool = True, whatif: bool = False) -> dict[str, Any]:
        if whatif:
            return {"Success": True, "whatif": True, "Entity": {"ID": client_id, **entity}, "Sparse": sparse}
        payload = {"Save": True, "Sparse": sparse, "Entity": entity}
        result = self.request("PUT", f"/Client/Put", json=payload)
        if result.get("Success") is True:
            return result
        return self.request("POST", f"/Client/Update", json=payload)

    def load_sales_reps(self) -> list[dict[str, Any]]:
        payload = self.request("POST", "/User/Lookup", json={"salesReps": True})
        data = payload.get("Data", [])
        rows: list[dict[str, Any]] = []
        for row in data:
            cleaned = _unwrap_aquira_value(row)
            if cleaned:
                rows.append(cleaned)
        return rows

    def logout(self) -> None:
        try:
            self.client.delete("/Session/Delete")
        except Exception:
            pass

    def close(self) -> None:
        self.client.close()
