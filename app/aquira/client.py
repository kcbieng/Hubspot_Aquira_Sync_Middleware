from __future__ import annotations

import logging
from typing import Any

import httpx

from app.aquira.normalize import (
    clients_from_contracts,
    list_from_envelope,
    merge_client,
    merge_contract,
    normalize_client,
    normalize_contact,
    normalize_contract,
    normalize_rep,
    normalize_spot_lines,
    unwrap_deep,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)

SESSION_ERROR_NAMES = {
    "sessionexpired",
    "unauthorized",
    "notloggedin",
    "notauthenticated",
    "invalidsession",
}


class AquiraApiError(RuntimeError):
    def __init__(self, message: str, *, error: Any = None, error_name: str | None = None, errors: Any = None):
        super().__init__(message)
        self.error = error
        self.error_name = error_name
        self.errors = errors


def _clean_secret(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    return text


def _login_error_message(data: dict[str, Any], status_code: int) -> str:
    parts = [str(part) for part in (data.get("ErrorName"), data.get("ErrorText"), data.get("Errors")) if part]
    if data.get("Error") not in (None, "", 0, "0"):
        parts.append(f"code={data.get('Error')}")
    if not parts:
        parts.append(f"Aquira login failed (HTTP {status_code})")
    return " | ".join(str(part) for part in parts)


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


def _session_lost(status_code: int, payload: dict[str, Any]) -> bool:
    if status_code == 401:
        return True
    name = str(payload.get("ErrorName") or "").lower().replace(" ", "")
    text = str(payload.get("ErrorText") or "").lower()
    if payload.get("Success") is False and (name in SESSION_ERROR_NAMES or "session" in text or "not logged" in text):
        return True
    return False


class AquiraSessionClient:
    def __init__(self, base_url: str | None = None, username: str | None = None, password: str | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.aquira_base_url).rstrip("/")
        self.username = username if username is not None else settings.aquira_username
        self.password = password if password is not None else settings.aquira_password
        self.client = httpx.Client(base_url=self.base_url, timeout=60.0, follow_redirects=True)
        self.logged_in = False
        self.version: str | None = None
        self._retrying = False

    def login(self) -> dict[str, Any]:
        user = (self.username or "").strip()
        password = _clean_secret(self.password or "")
        if not user or not password:
            raise AquiraApiError("Aquira username and password are required.")
        if password.startswith("gAAAA"):
            logger.error(
                "Aquira password looks like an encrypted settings blob, not the real password. "
                "Clear the UI-stored Aquira password or fix SETTINGS_FERNET_KEY."
            )
            raise AquiraApiError(
                "Aquira password could not be decrypted. Re-enter it in Settings or set AQUIRA_PASSWORD in the stack."
            )
        logger.info("Aquira login as %s (password_len=%s)", user, len(password))
        payload = {"Username": user, "Password": password}
        try:
            self.client.cookies.clear()
        except Exception:
            pass
        response = self.client.post("/Session/Post", json=payload)
        try:
            data = response.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        success = data.get("Success")
        accepted = response.status_code < 400 and success is not False and (
            success is True or bool(data.get("Entity") or data.get("SessionID"))
        )
        if accepted:
            self.logged_in = True
            entity = data.get("Entity") if isinstance(data.get("Entity"), dict) else {}
            self.version = str(entity.get("WebApiVersion") or entity.get("AquiraVersion") or data.get("name") or "") or None
            logger.info("Aquira session opened (version=%s)", self.version)
            return data
        raise AquiraApiError(
            _login_error_message(data, response.status_code),
            error=data.get("Error"),
            error_name=data.get("ErrorName"),
            errors=data.get("Errors"),
        )

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.client.request(method, path, **kwargs)
        try:
            payload = response.json() if getattr(response, "content", True) else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"Success": True, "Data": payload}
        if _session_lost(response.status_code, payload) and not self._retrying:
            self._retrying = True
            try:
                self.login()
                response = self.client.request(method, path, **kwargs)
            finally:
                self._retrying = False
            try:
                payload = response.json() if getattr(response, "content", True) else {}
            except Exception as exc:
                raise AquiraApiError(f"Aquira {method} {path} returned non-JSON (HTTP {response.status_code})") from exc
            if not isinstance(payload, dict):
                payload = {"Success": True, "Data": payload}
        elif not payload and response.content:
            try:
                payload = response.json()
            except Exception as exc:
                raise AquiraApiError(f"Aquira {method} {path} returned non-JSON (HTTP {response.status_code})") from exc
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
        except Exception as exc:
            text = str(exc)
            if "HTTP 5" in text or " 500" in text:
                logger.warning("Aquira %s %s failed: %s", method, path, exc)
            else:
                logger.debug("Aquira %s %s skipped: %s", method, path, exc)
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
        ident = int(client_id) if str(client_id).isdigit() else 0
        if ident <= 0:
            return []
        payload = self.try_request(
            "POST",
            "/Client/LookupContacts",
            json={"id": ident, "name": "lookup-contacts"},
        )
        if not payload:
            return []
        rows: list[dict[str, Any]] = []
        for row in list_from_envelope(payload):
            contact = normalize_contact(row, ident)
            if contact:
                rows.append(contact)
        return rows

    def search_clients(self, search_term: str = "") -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        get_all = self.try_request("GET", "/Client/Get")
        if get_all and list_from_envelope(get_all):
            payloads.append(get_all)
            logger.info("Client/Get returned %s rows", len(list_from_envelope(get_all)))
        search = self.try_request(
            "POST",
            "/Client/Search",
            json={"SearchTerm": search_term or "", "QuickSearchField": 1},
        )
        if search and list_from_envelope(search):
            payloads.append(search)
        if not payloads:
            for method, path, body in (
                ("POST", "/Client/Lookup", {"SearchTerm": search_term or "", "CurrentOnly": True}),
                ("POST", "/Client/AdvancedSearch", {"SearchTerm": search_term or ""}),
            ):
                payload = self.try_request(method, path, json=body)
                if payload and list_from_envelope(payload):
                    payloads.append(payload)
                    break
        by_id: dict[int, dict[str, Any]] = {}
        for payload in payloads:
            for row in list_from_envelope(payload):
                client = normalize_client(row)
                if client:
                    by_id[int(client["ID"])] = client
        return sorted(by_id.values(), key=lambda row: str(row.get("Name") or ""))

    def search_contracts(self, search_term: str = "") -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        search = self.try_request(
            "POST",
            "/Contract/Search",
            json={"SearchTerm": search_term or "", "IncludeActive": True, "IncludeInactive": False},
        )
        if search:
            payloads.append(search)
        if not search or not list_from_envelope(search):
            for method, path, body in (
                ("POST", "/Contract/Search", {"SearchTerm": search_term or "", "IncludeActive": True, "IncludeInactive": True}),
                ("POST", "/Contract/AdvancedSearch", {"SearchTerm": search_term, "CurrentOnly": True}),
                ("GET", "/Contract/Get", None),
                ("POST", "/Contract/Lookup", {"SearchTerm": search_term}),
            ):
                payload = self.try_request(method, path, json=body) if body is not None else self.try_request(method, path)
                if payload and list_from_envelope(payload):
                    payloads.append(payload)
                    break
        if search_term and str(search_term).isdigit():
            by_id = self.try_request("POST", "/Contract/SearchByID", json={"ID": int(search_term)})
            if by_id:
                payloads.append(by_id)
        by_id_map: dict[int, dict[str, Any]] = {}
        for payload in payloads:
            for row in list_from_envelope(payload):
                contract = normalize_contract(row)
                if contract:
                    by_id_map[int(contract["ID"])] = contract
        return list(by_id_map.values())

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
        payload = self.request("POST", f"/Contract/Load/{contract_id}", json={"name": "load"})
        lines = self.load_spot_lines(contract_id, payload)
        return normalize_contract(payload, lines)

    def load_sales_reps(self) -> list[dict[str, Any]]:
        if not self.logged_in:
            self.login()
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
        if not self.logged_in:
            self.login()
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
        clients_by_id: dict[int, dict[str, Any]] = {}
        for client in clients:
            try:
                loaded = self.load_client(client["ID"])
            except Exception as exc:
                logger.warning("Client/Load/%s failed: %s", client.get("ID"), exc)
                loaded = None
            merged = merge_client(client, loaded)
            if not merged:
                continue
            clients_by_id[int(merged["ID"])] = merged

        contracts = self.search_contracts(aquira_id or "")
        if aquira_id:
            contracts = [
                contract
                for contract in contracts
                if aquira_id
                in {
                    str(contract.get("ID")),
                    str(contract.get("AccountID")),
                    str(contract.get("AdvertiserID")),
                    str(contract.get("ContractCD")),
                }
            ]
        loaded_contracts: list[dict[str, Any]] = []
        for contract in contracts:
            try:
                loaded = self.load_contract(contract["ID"])
            except Exception as exc:
                logger.warning("Contract/Load/%s failed: %s", contract.get("ID"), exc)
                loaded = None
            merged = merge_contract(contract, loaded)
            if merged:
                loaded_contracts.append(merged)

        for stub in clients_from_contracts(loaded_contracts):
            existing = clients_by_id.get(int(stub["ID"]))
            clients_by_id[int(stub["ID"])] = merge_client(stub, existing) or stub

        loaded_clients = sorted(clients_by_id.values(), key=lambda row: str(row.get("Name") or ""))
        for client in loaded_clients:
            contacts.extend(client.get("Contacts") or [])
        reps = self.load_sales_reps()
        logger.info(
            "Aquira catalog ready: %s clients, %s contacts, %s contracts, %s reps",
            len(loaded_clients),
            len(contacts),
            len(loaded_contracts),
            len(reps),
        )
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

    def update_contact_sparse(self, client_id: str | int, contact_id: str | int, fields: dict[str, Any]) -> dict[str, Any]:
        loaded = self.request("POST", f"/Client/Load/{client_id}")
        entity = unwrap_deep(loaded.get("Entity") or loaded)
        if not isinstance(entity, dict):
            entity = {}
        contacts = list(entity.get("Contacts") or [])
        if not contacts:
            contacts = self.lookup_contacts(client_id)
        found = False
        wrapped: list[dict[str, Any]] = []
        for contact in contacts:
            row = dict(contact) if isinstance(contact, dict) else {}
            ident = unwrap_field_value(row.get("ID") or row.get("Id") or row.get("ContactID"))
            if str(ident) == str(contact_id):
                found = True
                for key, value in fields.items():
                    if value is None:
                        continue
                    row[key] = {"Value": value, "Valid": True}
            wrapped.append(
                {
                    "ID": ident,
                    "FirstName": {"Value": unwrap_field_value(row.get("FirstName")), "Valid": True},
                    "LastName": {"Value": unwrap_field_value(row.get("LastName")), "Valid": True},
                    "Email": {"Value": unwrap_field_value(row.get("Email")), "Valid": True},
                    "Phone": {"Value": unwrap_field_value(row.get("Phone")), "Valid": True},
                }
            )
        if not found:
            raise AquiraApiError(f"Aquira contact {contact_id} was not found on client {client_id}")
        sparse: dict[str, Any] = {
            "ID": int(client_id) if str(client_id).isdigit() else client_id,
            "Contacts": wrapped,
        }
        if entity.get("Version") is not None:
            sparse["Version"] = entity.get("Version")
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
    except AquiraApiError as exc:
        hint = None
        if exc.error in (-7, "-7") or (exc.error_name or "").lower() == "loginfailed":
            hint = (
                f"Aquira rejected user {settings.aquira_username!r} (LoginFailed / -7). "
                "This is Aquira auth, not HubSpot. Check that the user is Current/Enabled, "
                "not locked after failed logins, and that the password in Portainer or Settings "
                "matches. Confirm by logging into Aquira as that user, then Test Aquira again."
            )
        return {
            "status": "error",
            "mode": "live",
            "message": str(exc),
            "error": exc.error,
            "error_name": exc.error_name,
            "errors": exc.errors,
            "username": settings.aquira_username,
            "hint": hint,
        }
    except Exception as exc:
        return {"status": "error", "mode": "live", "message": str(exc)}
    finally:
        client.close()
