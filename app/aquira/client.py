from __future__ import annotations

from typing import Any

import httpx

from app.aquira.normalize import (
    list_from_envelope,
    normalize_client,
    normalize_contact,
    normalize_contract,
    normalize_rep,
    normalize_spot_lines,
    unwrap_deep,
)
from app.settings import get_settings


class AquiraApiError(RuntimeError):
    def __init__(self, message: str, *, error: Any = None, error_name: str | None = None, errors: Any = None):
        super().__init__(message)
        self.error = error
        self.error_name = error_name
        self.errors = errors


def unwrap_field_value(value: Any) -> Any:
    if isinstance(value, dict) and "Value" in value:
        return unwrap_field_value(value.get("Value"))
    return value


def validate_response(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("Success") is False:
        raise AquiraApiError(
            payload.get("ErrorText")
            or payload.get("ErrorName")
            or f"Aquira request failed: Error={payload.get('Error')}; Errors={payload.get('Errors')}; ErrorName={payload.get('ErrorName')}",
            error=payload.get("Error"),
            error_name=payload.get("ErrorName"),
            errors=payload.get("Errors"),
        )
    return payload


class AquiraSessionClient:
    def __init__(self, base_url: str | None = None, username: str | None = None, password: str | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.aquira_base_url).rstrip("/")
        self.username = username if username is not None else settings.aquira_username
        self.password = password if password is not None else settings.aquira_password
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0, follow_redirects=True)
        self.logged_in = False
        self.version: str | None = None

    def login(self) -> dict[str, Any]:
        if not self.username or not self.password:
            raise AquiraApiError("Aquira username and password are required.")
        response = self.client.post("/Session/Post", json={"UserName": self.username, "Password": self.password})
        try:
            data = response.json()
        except Exception as exc:
            raise AquiraApiError(f"Aquira login returned non-JSON (HTTP {response.status_code})") from exc
        if response.status_code >= 400 or data.get("Success") is False:
            raise AquiraApiError(
                data.get("ErrorText") or data.get("ErrorName") or f"Aquira login failed (HTTP {response.status_code})",
                error=data.get("Error"),
                error_name=data.get("ErrorName"),
                errors=data.get("Errors"),
            )
        validate_response(data)
        self.logged_in = True
        entity = data.get("Entity") if isinstance(data.get("Entity"), dict) else {}
        self.version = str(entity.get("WebApiVersion") or entity.get("AquiraVersion") or data.get("name") or "") or None
        return data

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.client.request(method, path, **kwargs)
        if response.status_code == 401:
            self.login()
            response = self.client.request(method, path, **kwargs)
        try:
            payload = response.json() if getattr(response, "content", True) else {}
        except Exception as exc:
            raise AquiraApiError(f"Aquira {method} {path} returned non-JSON (HTTP {response.status_code})") from exc
        if not isinstance(payload, dict):
            payload = {"Success": True, "Data": payload}
        if response.status_code >= 400 or payload.get("Success") is False:
            raise AquiraApiError(
                payload.get("ErrorText")
                or payload.get("ErrorName")
                or f"Aquira {method} {path} failed (HTTP {response.status_code})",
                error=payload.get("Error"),
                error_name=payload.get("ErrorName"),
                errors=payload.get("Errors"),
            )
        return validate_response(payload)

    def try_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
        try:
            return self.request(method, path, **kwargs)
        except Exception:
            return None

    def heartbeat(self) -> bool:
        try:
            response = self.client.head("/User/HeartBeat")
            return response.status_code in (200, 204)
        except Exception:
            return False

    def version_info(self) -> str:
        try:
            payload = self.request("GET", "/AquiraAPI/Version")
            entity = payload.get("Entity") if isinstance(payload.get("Entity"), dict) else {}
            version = str(entity.get("WebApiVersion") or entity.get("AquiraVersion") or entity.get("Version") or payload.get("name") or "")
            self.version = version or self.version
            return self.version or "ok"
        except Exception:
            return self.version or "ok"

    def load_client(self, client_id: str | int) -> dict[str, Any]:
        payload = self.request("POST", f"/Client/Load/{client_id}")
        client = normalize_client(payload) or {}
        if client and not client.get("Contacts"):
            contacts = self.lookup_contacts(client_id)
            if contacts:
                client["Contacts"] = contacts
        return client

    def lookup_contacts(self, client_id: str | int) -> list[dict[str, Any]]:
        payload = self.try_request("POST", "/Client/LookupContacts", json={"ClientID": int(client_id) if str(client_id).isdigit() else client_id, "SearchTerm": ""})
        if not payload:
            return []
        rows: list[dict[str, Any]] = []
        for row in list_from_envelope(payload):
            contact = normalize_contact(row, int(client_id) if str(client_id).isdigit() else 0)
            if contact:
                rows.append(contact)
        return rows

    def search_clients(self, search_term: str = "") -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        search = self.try_request("POST", "/Client/Search", json={"SearchTerm": search_term, "CurrentOnly": True})
        if search:
            payloads.append(search)
        if not search or not list_from_envelope(search):
            for method, path, body in (
                ("POST", "/Client/AdvancedSearch", {"SearchTerm": search_term, "CurrentOnly": True}),
                ("GET", "/Client/Get", None),
                ("POST", "/Client/Lookup", {"SearchTerm": search_term}),
            ):
                payload = self.try_request(method, path, json=body) if body is not None else self.try_request(method, path)
                if payload:
                    payloads.append(payload)
        by_id: dict[int, dict[str, Any]] = {}
        for payload in payloads:
            for row in list_from_envelope(payload):
                client = normalize_client(row)
                if client:
                    by_id[int(client["ID"])] = client
        return sorted(by_id.values(), key=lambda row: str(row.get("Name") or ""))

    def search_contracts(self, search_term: str = "") -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        search = self.try_request("POST", "/Contract/Search", json={"SearchTerm": search_term, "CurrentOnly": True})
        if search:
            payloads.append(search)
        if not search or not list_from_envelope(search):
            for method, path, body in (
                ("POST", "/Contract/AdvancedSearch", {"SearchTerm": search_term, "CurrentOnly": True}),
                ("GET", "/Contract/Get", None),
                ("POST", "/Contract/Lookup", {"SearchTerm": search_term}),
            ):
                payload = self.try_request(method, path, json=body) if body is not None else self.try_request(method, path)
                if payload:
                    payloads.append(payload)
        by_id: dict[int, dict[str, Any]] = {}
        for payload in payloads:
            for row in list_from_envelope(payload):
                contract = normalize_contract(row)
                if contract:
                    by_id[int(contract["ID"])] = contract
        return list(by_id.values())

    def load_spot_lines(self, contract_id: str | int, loaded: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from_load = normalize_spot_lines(loaded) if loaded else []
        if from_load:
            return from_load
        attempts = [
            ("POST", "/Contract/GetSpotLineDetailAnalysis", {"ContractID": int(contract_id) if str(contract_id).isdigit() else contract_id}),
            ("POST", f"/Contract/GetSpotLineDetailAnalysis/{contract_id}", None),
            ("POST", "/Contract/LoadSpotline", {"ContractID": int(contract_id) if str(contract_id).isdigit() else contract_id}),
            ("POST", f"/Contract/LoadSpotline/{contract_id}", None),
            ("POST", "/Contract/GetContractDetailAnalysis", {"ContractID": int(contract_id) if str(contract_id).isdigit() else contract_id}),
            ("POST", "/Contract/LoadSpotlineStationSpots", {"ContractID": int(contract_id) if str(contract_id).isdigit() else contract_id}),
        ]
        for method, path, body in attempts:
            payload = self.try_request(method, path, json=body) if body is not None else self.try_request(method, path)
            if not payload:
                continue
            lines = normalize_spot_lines(payload)
            if lines:
                return lines
        return []

    def load_contract(self, contract_id: str | int) -> dict[str, Any] | None:
        payload = self.request("POST", f"/Contract/Load/{contract_id}")
        lines = self.load_spot_lines(contract_id, payload)
        return normalize_contract(payload, lines)

    def load_sales_reps(self) -> list[dict[str, Any]]:
        payload = self.try_request("POST", "/User/Lookup", json={"salesReps": True, "CurrentOnly": True, "SearchTerm": ""})
        if not payload:
            payload = self.try_request("POST", "/User/Lookup", json={"salesReps": True})
        if not payload:
            return []
        by_id: dict[str, dict[str, Any]] = {}
        for row in list_from_envelope(payload) or payload.get("Data") or []:
            rep = normalize_rep(row)
            if rep:
                by_id[str(rep["id"])] = rep
        return list(by_id.values())

    def load_catalog(self, aquira_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        clients = self.search_clients(aquira_id or "")
        if aquira_id:
            needle = aquira_id.lower()
            filtered = [
                client
                for client in clients
                if str(client.get("ID")) == aquira_id or needle in str(client.get("Name") or "").lower()
            ]
            if not filtered:
                loaded = self.load_client(aquira_id)
                filtered = [loaded] if loaded else []
            clients = filtered
        loaded_clients: list[dict[str, Any]] = []
        contacts: list[dict[str, Any]] = []
        for client in clients:
            try:
                full = self.load_client(client["ID"]) or client
            except Exception:
                full = client
            loaded_clients.append(full)
            contacts.extend(full.get("Contacts") or [])

        contracts = self.search_contracts(aquira_id or "")
        if aquira_id:
            contracts = [
                contract
                for contract in contracts
                if aquira_id in {str(contract.get("ID")), str(contract.get("AccountID")), str(contract.get("AdvertiserID")), str(contract.get("ContractCD"))}
            ]
        loaded_contracts: list[dict[str, Any]] = []
        for contract in contracts:
            try:
                loaded_contracts.append(self.load_contract(contract["ID"]) or contract)
            except Exception:
                loaded_contracts.append(contract)
        reps = self.load_sales_reps()
        return {"clients": loaded_clients, "contacts": contacts, "contracts": loaded_contracts, "reps": reps}

    def update_client_sparse(self, aquira_id: str | int, fields: dict[str, Any]) -> dict[str, Any]:
        loaded = self.request("POST", f"/Client/Load/{aquira_id}")
        entity = unwrap_deep(loaded.get("Entity") or loaded)
        if not isinstance(entity, dict):
            entity = {}
        sparse: dict[str, Any] = {"ID": int(aquira_id) if str(aquira_id).isdigit() else aquira_id}
        if entity.get("Version") is not None:
            sparse["Version"] = entity.get("Version")
        for key, value in fields.items():
            if value is None:
                continue
            sparse[key] = {"Value": value, "Valid": True}
        return self.request("PUT", "/Client/Put", json={"Save": True, "Sparse": True, "Entity": sparse})

    def create_client(self, fields: dict[str, Any]) -> dict[str, Any]:
        created = self.try_request(
            "POST",
            "/Client/Create",
            json={"Entity": {"Name": fields.get("Name"), "IsAccount": True, "IsAdvertiser": False}},
        ) or self.request("POST", "/Client/Create", json={})
        draft = normalize_client(created) or {}
        ident = draft.get("ID")
        if not ident:
            raise AquiraApiError("Aquira Client/Create did not return an ID")
        self.update_client_sparse(ident, fields)
        loaded = self.load_client(ident)
        if not loaded:
            raise AquiraApiError("Created Aquira client could not be reloaded")
        return loaded

    def logout(self) -> None:
        try:
            self.client.delete("/Session/Delete")
        except Exception:
            pass
        finally:
            self.logged_in = False

    def close(self) -> None:
        try:
            if self.logged_in:
                self.logout()
        finally:
            self.client.close()


def test_aquira_connection(settings=None) -> dict[str, Any]:
    settings = settings or get_settings()
    client = AquiraSessionClient(
        base_url=settings.aquira_base_url,
        username=settings.aquira_username,
        password=settings.aquira_password,
    )
    try:
        client.login()
        version = client.version_info()
        beat = client.heartbeat()
        return {
            "status": "ok",
            "mode": "live",
            "message": "Aquira session accepted." if beat else "Logged in; heartbeat was inconclusive.",
            "version": version,
        }
    except Exception as exc:
        return {"status": "error", "mode": "live", "message": str(exc)}
    finally:
        client.close()
