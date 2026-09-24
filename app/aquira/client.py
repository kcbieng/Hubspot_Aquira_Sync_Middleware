from __future__ import annotations

import logging
from typing import Any

import httpx

from app.aquira.normalize import (
    clients_from_contracts,
    list_from_envelope,
    merge_client,
    merge_contract,
    normalize_charge_lines,
    normalize_client,
    normalize_contact,
    normalize_contract,
    normalize_rep,
    normalize_revenue_months,
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

# Verified against the live tenant and the raw /swagger/docs/v1 spec: every
# Aquira enumeration returns exactly this many rows and the spec declares no
# paging parameter for any of them.
TRUNCATION_SENTINEL = 100


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
        self.failed_calls: list[dict[str, Any]] = []
        self.truncated_sources: list[str] = []

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
            payload = self.request(method, path, **kwargs)
        except Exception as exc:
            text = str(exc)
            self.failed_calls.append(
                {"method": method, "path": path, "error": type(exc).__name__, "message": text[:300]}
            )
            if "HTTP 5" in text or " 500" in text:
                logger.warning("Aquira %s %s failed: %s", method, path, exc)
            else:
                logger.debug("Aquira %s %s skipped: %s", method, path, exc)
            return None
        rows = len(list_from_envelope(payload))
        if rows >= TRUNCATION_SENTINEL:
            # The raw Swagger spec declares no paging parameter anywhere, and
            # /Client/Get and /Contract/Get take no parameters at all, so a
            # result that lands exactly on the server's cap is truncated and
            # indistinguishable from a complete answer. Nothing may treat this
            # pull as authoritative.
            self.truncated_sources.append(f"{method} {path}")
            logger.warning("Aquira %s %s returned %s rows — server cap, results truncated", method, path, rows)
        return payload

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

    def _client_matches(self, client: dict[str, Any], query: str) -> bool:
        needle = str(query or "").strip().lower()
        if not needle:
            return True
        exact = {
            str(client.get("ID") or "").strip().lower(),
            str(client.get("ClientCD") or "").strip().lower(),
        }
        if needle in exact:
            return True
        for field in ("Name", "ShortName", "LongName"):
            value = str(client.get(field) or "").strip().lower()
            if value and (needle == value or needle in value):
                return True
        return False

    def resolve_clients(self, query: str) -> list[dict[str, Any]]:
        needle = str(query or "").strip()
        rows = self.search_clients(needle)
        matches = [row for row in rows if self._client_matches(row, needle)]
        if matches:
            logger.info("Resolved Aquira client query %r to %s row(s) by id/cd/name", needle, len(matches))
            return matches
        if needle.isdigit():
            by_id = self.try_request("POST", "/Client/SearchByID", json={"SearchIDs": [int(needle)], "name": "by-id"})
            for row in list_from_envelope(by_id or {}):
                client = normalize_client(row)
                if client:
                    matches.append(client)
            if matches:
                logger.info("Resolved Aquira client query %r via SearchByID", needle)
                return matches
        lookup = self.try_request(
            "POST",
            "/Client/Lookup",
            json={"SearchTerm": needle, "CurrentOnly": True, "accounts": True, "advertisers": True, "name": "lookup"},
        )
        for row in list_from_envelope(lookup or {}):
            client = normalize_client(row)
            if client and self._client_matches(client, needle):
                matches.append(client)
        if matches:
            logger.info("Resolved Aquira client query %r via Lookup", needle)
            return matches
        if needle.isdigit():
            loaded = self.try_request("POST", f"/Client/Load/{needle}")
            client = normalize_client(loaded) if loaded else None
            if client:
                logger.info("Resolved Aquira client query %r via Load/%s", needle, needle)
                return [client]
        logger.warning(
            "No Aquira client matched %r. The Aquira UI Client ID is ClientCD; /Client/Load uses the internal ID.",
            needle,
        )
        return []

    def search_clients(self, search_term: str = "") -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        get_all = self.try_request("GET", "/Client/Get")
        if get_all and list_from_envelope(get_all):
            payloads.append(get_all)
            logger.info("Client/Get returned %s rows", len(list_from_envelope(get_all)))
        # Live semantics matrix: QSF 0-4 match no non-empty term at all; 5 is the
        # numeric ClientCD, 6/7 name text, 8 matches either and is what this
        # tenant's own UI (AppSettings.ClientQuickSearchField) uses. QSF=1 was
        # silently dead — it only ever returned rows for the empty term.
        search = self.try_request(
            "POST",
            "/Client/Search",
            json={"SearchTerm": search_term or "", "QuickSearchField": 8},
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
        attempts: list[tuple[str, str, dict[str, Any] | None]] = [
            (
                "POST",
                "/Contract/Search",
                {"SearchTerm": search_term or "", "IncludeActive": True, "IncludeInactive": True},
            ),
            (
                "POST",
                "/Contract/Lookup",
                {"SearchTerm": search_term or "", "IncludeStatuses": [0, 1, 2, 3, 4, 5]},
            ),
            ("GET", "/Contract/Get", None),
        ]
        if search_term and str(search_term).isdigit():
            attempts.append(("POST", "/Contract/SearchByID", {"SearchIDs": [int(search_term)]}))
        for method, path, body in attempts:
            payload = self.try_request(method, path, json=body) if body is not None else self.try_request(method, path)
            rows = list_from_envelope(payload) if payload else []
            if rows:
                payloads.append(payload)
                logger.info("Aquira %s returned %s contract/proposal rows", path, len(rows))
        by_id_map: dict[int, dict[str, Any]] = {}
        for payload in payloads:
            for row in list_from_envelope(payload):
                contract = normalize_contract(row)
                if not contract:
                    continue
                ident = int(contract["ID"])
                existing = by_id_map.get(ident)
                if existing is None:
                    by_id_map[ident] = contract
                    continue
                for key, value in contract.items():
                    if key in {"IsContract", "IsProposal", "Cancelled"}:
                        existing[key] = bool(existing.get(key)) or bool(value)
                    elif value not in (None, "", [], 0, 0.0) and existing.get(key) in (None, "", [], 0, 0.0):
                        existing[key] = value
        booked = sum(1 for row in by_id_map.values() if row.get("IsContract"))
        proposals = sum(1 for row in by_id_map.values() if row.get("IsProposal") and not row.get("IsContract"))
        logger.info("Aquira search combined %s unique deals (%s booked, %s proposal)", len(by_id_map), booked, proposals)
        return list(by_id_map.values())

    # Verified against the live tenant: every enumeration endpoint caps at
    # TRUNCATION_SENTINEL rows, and none of them read any paging parameter —
    # 23 single names + 12 combinations, as query string and as body fields,
    # all return a byte-identical ID set. The /Forecast/Search OffSet crash at
    # out-of-range offsets proves the probe would have found hidden paging.
    # SearchByID inverts the cap: the CALLER chooses the ID list, so a batch of
    # SWEEP_BATCH requested IDs can never reach the cap, and completeness
    # becomes a checkable property instead of an assumption.
    SWEEP_BATCH = 50
    SWEEP_DEAD_TAIL_RUNS = 4  # stop only after 4 consecutive fully-dead batches
    SWEEP_MAX_BATCHES = 400   # 20k-ID ceiling; hitting it is a partial sweep

    def sweep_enumerate(self, resource: str) -> tuple[list[dict[str, Any]], bool]:
        """Enumerate every existing {resource} row via SearchByID batches.

        Live-tenant fact that shapes this (probe 2026-09-24): a SearchByID batch in
        which NO requested id exists is answered `HTTP 200` + `Success:false` +
        `ErrorName:"NotFound"` + `Error:-12`, NOT with an empty Data list. request()
        raises on Success:false, try_request returns None, and the loop used to break
        right there — so the dead-tail proof could never be satisfied and EVERY full
        run was reported PARTIAL at exactly the first fully-dead id range, for both
        resources. Zero-match is now unreachable: each batch carries a sentinel id
        known to exist (the doubling probe's high-water), so an all-dead range reads
        as "only the sentinel came back" instead of as an error. Proven live: 49
        non-existent ids + 1 live id -> Success:true, exactly the live row.

        The sentinel is never counted as a hit (only ids inside the batch's own range
        are), and it rides along as a legitimate row in the result set. If the sentinel
        itself is deleted mid-run, its batch goes zero-match again, raises, and the
        sweep fails closed as PARTIAL — the correct answer, since nothing can then tell
        "deleted" from "endpoint broken".

        Returns (rows, complete). complete is False — and the source is flagged in
        truncated_sources — whenever the tail was not proven dead, a batch returned a
        row whose id was not requested (the server is not honoring the ID list), or a
        batch failed. Row count is NOT the tell: SearchByID is not row-capped (400
        requested ids returned 279 rows), so the only sound integrity check is that the
        returned ids are a subset of the requested ones. Residual risk that cannot be
        eliminated from the outside: a deletion burst of >200 IDs followed by creates at
        far higher IDs; sequential ID allocation makes that effectively impossible.
        """
        path = f"/{resource}/SearchByID"

        def ident_of(row: Any) -> int | None:
            if not isinstance(row, dict):
                return None
            ident = row.get("ID") or row.get("Id") or row.get("id")
            return int(ident) if str(ident or "").isdigit() else None

        rows_by_id: dict[int, dict[str, Any]] = {}

        def hits_of(payload: dict[str, Any], allowed: set[int]) -> tuple[list[int | None], int]:
            """Row ids plus the count of rows the server was NOT asked for — including
            rows with no usable id, which cannot be attributed to a request either."""
            idents = [ident_of(row) for row in list_from_envelope(payload)]
            return idents, sum(1 for ident in idents if ident not in allowed)

        powers = [2 ** k for k in range(0, 17)]
        anchor = 0
        probe = self.try_request("POST", path, json={"SearchIDs": powers})
        if probe is not None:
            # The anchor is checked like any other batch: an id list the server did not
            # honor here would otherwise become a sentinel that absorbs later strays.
            idents, stray = hits_of(probe, set(powers))
            if stray or len(idents) >= TRUNCATION_SENTINEL:
                self.truncated_sources.append(
                    f"POST {path} anchor probe returned {len(idents)} rows for {len(powers)} "
                    f"requested IDs ({stray} not requested) — server ignored the ID list; "
                    "sweep cannot self-certify"
                )
                return [], False
            anchor = max([a for a in idents if a] or [0])
        sentinel = anchor or None
        start = 1
        empty_run = batches = 0
        while batches < self.SWEEP_MAX_BATCHES:
            ids = list(range(start, start + self.SWEEP_BATCH))
            in_batch = set(ids)
            seed = sentinel if sentinel is not None and sentinel not in in_batch else None
            payload = self.try_request(
                "POST", path, json={"SearchIDs": [*ids, seed] if seed is not None else ids}
            )
            if payload is None:
                break
            requested = in_batch | ({seed} if seed is not None else set())
            hits, stray = hits_of(payload, requested)
            if stray or len(hits) >= TRUNCATION_SENTINEL:
                self.truncated_sources.append(
                    f"POST {path} returned {len(hits)} rows for {len(requested)} requested IDs "
                    f"({stray} not requested) — server ignored the ID list; sweep cannot self-certify"
                )
                return list(rows_by_id.values()), False
            for row, ident in zip(list_from_envelope(payload), hits):
                if ident is not None:
                    rows_by_id[ident] = row
            batches += 1
            start += self.SWEEP_BATCH
            # A tenant with no power of two among its live ids leaves the doubling probe
            # empty; adopt the highest id actually seen so the tail stays reachable.
            if sentinel is None and rows_by_id:
                sentinel = max(rows_by_id)
            if sum(1 for ident in hits if ident in in_batch):
                empty_run = 0
            else:
                empty_run += 1
                # rows_by_id must be non-empty: a tenant whose first live ID sits
                # past the dead-tail window would otherwise be "proven" empty
                # without the sweep having seen anything at all.
                if (
                    rows_by_id
                    and empty_run >= self.SWEEP_DEAD_TAIL_RUNS
                    and start - 1 >= 2 * anchor
                ):
                    logger.info("Aquira %s sweep complete: %s rows below %s", path, len(rows_by_id), start - 1)
                    return list(rows_by_id.values()), True
        self.truncated_sources.append(
            f"POST {path} sweep stopped at ID {start - 1} unproven (anchor {anchor}, {batches} batches)"
        )
        logger.warning("Aquira %s sweep PARTIAL at ID %s (%s rows)", path, start - 1, len(rows_by_id))
        return list(rows_by_id.values()), False

    def enumerate_clients(self) -> tuple[list[dict[str, Any]], bool]:
        rows, complete = self.sweep_enumerate("Client")
        clients = [c for c in (normalize_client(row) for row in rows) if c and str(c.get("ID") or "").isdigit()]
        if len(clients) < len(rows):
            # A sweep row that will not normalize is an entity this run cannot see.
            # "Complete enumeration" must mean ALL of it or it means nothing.
            self.truncated_sources.append(
                f"Client sweep: {len(rows) - len(clients)} SearchByID row(s) did not normalize"
            )
        if not clients and not complete:
            # Sweep found nothing AND could not prove the table empty: fall back
            # to the capped union so its truncation at least gets reported.
            return self.search_clients(""), False
        return clients, complete

    def enumerate_contracts(self) -> tuple[list[dict[str, Any]], bool]:
        rows, complete = self.sweep_enumerate("Contract")
        contracts = [c for c in (normalize_contract(row) for row in rows) if c and str(c.get("ID") or "").isdigit()]
        if len(contracts) < len(rows):
            self.truncated_sources.append(
                f"Contract sweep: {len(rows) - len(contracts)} SearchByID row(s) did not normalize"
            )
        booked = sum(1 for row in contracts if row.get("IsContract"))
        proposals = sum(1 for row in contracts if row.get("IsProposal") and not row.get("IsContract"))
        logger.info(
            "Aquira contract sweep: %s deals (%s booked, %s proposal), complete=%s",
            len(contracts), booked, proposals, complete,
        )
        if not contracts and not complete:
            return self.search_contracts(""), False
        return contracts, complete

    def load_spot_lines(self, contract_id: str | int, loaded: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from_load = normalize_spot_lines(loaded) if loaded else []
        ident = int(contract_id) if str(contract_id).isdigit() else contract_id
        attempts = [
            ("POST", "/Contract/GetSpotLineDetailAnalysis", {"id": ident, "ID": ident, "name": "spot-lines"}),
            ("POST", "/Contract/LoadSpotline", {"ContractID": ident, "name": "spotline"}),
        ]
        for method, path, body in attempts:
            payload = self.try_request(method, path, json=body)
            if not payload:
                continue
            lines = normalize_spot_lines(payload)
            if lines:
                return lines
        return from_load

    def load_charge_lines(self, contract_id: str | int, loaded: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        charges = normalize_charge_lines(loaded) if loaded else []
        if charges:
            return charges
        ident = int(contract_id) if str(contract_id).isdigit() else contract_id
        payload = self.try_request(
            "POST",
            "/Contract/GetContractDetailAnalysis",
            json={"ID": ident, "id": ident, "RevenueDateType": 0, "name": "detail"},
        )
        return normalize_charge_lines(payload) if payload else []

    def load_revenue_months(self, contract_id: str | int) -> list[dict[str, Any]]:
        ident = int(contract_id) if str(contract_id).isdigit() else contract_id
        payload = self.try_request(
            "POST",
            "/Contract/GetContractDetailAnalysis",
            json={"ID": ident, "id": ident, "RevenueDateType": 0, "name": "detail"},
        )
        return normalize_revenue_months(payload) if payload else []

    def load_contract(self, contract_id: str | int) -> dict[str, Any] | None:
        payload = self.request("POST", f"/Contract/Load/{contract_id}", json={"name": "load"})
        months = self.load_revenue_months(contract_id)
        if months:
            return normalize_contract(payload, months)
        lines = self.load_spot_lines(contract_id, payload)
        charges = self.load_charge_lines(contract_id, payload)
        return normalize_contract(payload, [*lines, *charges])


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

    def load_catalog(self, aquira_id: str | None = None) -> dict[str, Any]:
        if not self.logged_in:
            self.login()
        self.failed_calls = []
        self.truncated_sources = []
        clients_complete = True
        if aquira_id:
            clients = self.resolve_clients(aquira_id)
        else:
            clients, clients_complete = self.enumerate_clients()
        loaded_clients: list[dict[str, Any]] = []
        contacts: list[dict[str, Any]] = []
        clients_by_id: dict[int, dict[str, Any]] = {}
        for client in clients:
            ident = client.get("ID")
            if not ident:
                continue
            try:
                loaded = self.load_client(ident)
            except Exception as exc:
                logger.warning("Client/Load/%s failed: %s", ident, exc)
                loaded = None
            merged = merge_client(client, loaded)
            if not merged:
                continue
            clients_by_id[int(merged["ID"])] = merged

        contracts_complete = True
        if aquira_id:
            contracts = self.search_contracts(aquira_id)
            client_ids = {str(client_id) for client_id in clients_by_id}
            client_cds = {str(row.get("ClientCD") or "") for row in clients_by_id.values()}
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
                or str(contract.get("AccountID")) in client_ids
                or str(contract.get("AdvertiserID")) in client_ids
                or str(contract.get("ContractCD")) in client_cds
            ]
        else:
            contracts, contracts_complete = self.enumerate_contracts()
        loaded_contracts: list[dict[str, Any]] = []
        for contract in contracts:
            detail_failed = False
            try:
                loaded = self.load_contract(contract["ID"])
            except Exception as exc:
                logger.warning("Contract/Load/%s failed: %s", contract.get("ID"), exc)
                detail_failed = True
                loaded = None
            merged = merge_contract(contract, loaded)
            if merged:
                # A contract that appears in the pull but whose lines could not be
                # read must never be allowed to prune HubSpot — zero periods out of
                # a thin read is indistinguishable from "the flight moved months".
                merged["_detail_failed"] = detail_failed
                loaded_contracts.append(merged)

        for stub in clients_from_contracts(loaded_contracts):
            existing = clients_by_id.get(int(stub["ID"]))
            clients_by_id[int(stub["ID"])] = merge_client(stub, existing) or stub

        loaded_clients = sorted(clients_by_id.values(), key=lambda row: str(row.get("Name") or ""))
        for client in loaded_clients:
            contacts.extend(client.get("Contacts") or [])
        reps = self.load_sales_reps()
        logger.info(
            "Aquira catalog ready: %s clients, %s contacts, %s deals (%s booked / %s proposal), %s reps",
            len(loaded_clients),
            len(contacts),
            len(loaded_contracts),
            sum(1 for row in loaded_contracts if row.get("IsContract")),
            sum(1 for row in loaded_contracts if row.get("IsProposal") and not row.get("IsContract")),
            len(reps),
        )
        return {
            "clients": loaded_clients,
            "contacts": contacts,
            "contracts": loaded_contracts,
            "reps": reps,
            "_integrity": {
                "failed_reads": len(self.failed_calls),
                "failed_calls": list(self.failed_calls),
                "truncated_sources": list(self.truncated_sources),
                "detail_failures": sum(1 for row in loaded_contracts if row.get("_detail_failed")),
                "contract_rows": len(loaded_contracts),
                "client_rows": len(loaded_clients),
                "enumeration": {
                    "clients_complete": clients_complete,
                    "contracts_complete": contracts_complete,
                },
                # Aquira has no pagination, no totalCount and no modified-since. The
                # SearchByID sweep is the only self-certifying enumeration, so a full
                # run is authoritative only when both sweeps proved the ID tail dead.
                # A targeted (aquira_id) run keeps completeness vacuously True because
                # pruning is already scoped out by the caller.
                "certified": (
                    clients_complete
                    and contracts_complete
                    and not self.failed_calls
                    and not self.truncated_sources
                ),
            },
        }

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

    def create_client(self, fields: dict[str, Any], party_type: str = "account") -> dict[str, Any]:
        party = str(party_type or "").strip().lower()
        is_advertiser = party in {"advertiser", "both"}
        is_account = party in {"account", "both"} or not party
        created = self.try_request(
            "POST",
            "/Client/Create",
            json={
                "Entity": {
                    "Name": fields.get("Name"),
                    "IsAccount": is_account,
                    "IsAdvertiser": is_advertiser,
                }
            },
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
