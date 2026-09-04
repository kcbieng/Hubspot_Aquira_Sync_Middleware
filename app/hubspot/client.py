from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any

import httpx

from app.aquira.contracts import normalize_spotlines
from app.hashutil import content_hash
from app.mapping.revenue import allocate_revenue
from app.settings import get_settings

logger = logging.getLogger(__name__)

BOOL_OPTIONS = [
    {"label": "Yes", "value": "true", "displayOrder": 0, "hidden": False},
    {"label": "No", "value": "false", "displayOrder": 1, "hidden": False},
]


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "Value" in value:
        return value.get("Value")
    return value


class HubSpotApiError(RuntimeError):
    def __init__(self, status: int, body: str = "", message: str | None = None):
        super().__init__(message or f"HubSpot HTTP {status}")
        self.status = status
        self.body = body


COMPANY_PROPS = [
    {"name": "aquira_id", "label": "Aquira ID", "type": "string", "fieldType": "text", "hasUniqueValue": True},
    {
        "name": "aquira_party_type",
        "label": "Aquira party type",
        "type": "enumeration",
        "fieldType": "select",
        "options": [
            {"label": "Account", "value": "account"},
            {"label": "Advertiser", "value": "advertiser"},
            {"label": "Both", "value": "both"},
            {"label": "Unknown", "value": "unknown"},
        ],
    },
    {"name": "aquira_version", "label": "Aquira version", "type": "number", "fieldType": "number"},
]
CONTACT_PROPS = [
    {"name": "aquira_id", "label": "Aquira ID", "type": "string", "fieldType": "text", "hasUniqueValue": True},
    {"name": "aquira_entity_type", "label": "Aquira entity type", "type": "string", "fieldType": "text"},
    {"name": "aquira_client_id", "label": "Aquira client ID", "type": "string", "fieldType": "text"},
]
DEAL_PROPS = [
    {"name": "aquira_id", "label": "Aquira ID", "type": "string", "fieldType": "text", "hasUniqueValue": True},
    {"name": "aquira_contract_cd", "label": "Aquira contract CD", "type": "string", "fieldType": "text"},
    {"name": "aquira_status", "label": "Aquira status", "type": "string", "fieldType": "text"},
    {"name": "aquira_is_proposal", "label": "Aquira is proposal", "type": "bool", "fieldType": "booleancheckbox", "options": BOOL_OPTIONS},
    {"name": "aquira_is_contract", "label": "Aquira is contract", "type": "bool", "fieldType": "booleancheckbox", "options": BOOL_OPTIONS},
    {"name": "aquira_sign_date", "label": "Aquira sign date", "type": "date", "fieldType": "date"},
    {"name": "aquira_start_date", "label": "Aquira start date", "type": "date", "fieldType": "date"},
    {"name": "aquira_end_date", "label": "Aquira end date", "type": "date", "fieldType": "date"},
    {"name": "aquira_stations", "label": "Aquira stations", "type": "string", "fieldType": "text"},
    {"name": "aquira_account_id", "label": "Aquira account ID", "type": "string", "fieldType": "text"},
    {"name": "aquira_advertiser_id", "label": "Aquira advertiser ID", "type": "string", "fieldType": "text"},
    {"name": "aquira_sales_rep", "label": "Aquira sales rep", "type": "string", "fieldType": "text"},
    {"name": "aquira_allocated_amount", "label": "Aquira allocated amount", "type": "number", "fieldType": "number"},
    {"name": "aquira_line_total", "label": "Aquira line total", "type": "number", "fieldType": "number"},
    {"name": "aquira_spot_total", "label": "Aquira spot total", "type": "number", "fieldType": "number"},
    {"name": "aquira_charge_total", "label": "Aquira charge total", "type": "number", "fieldType": "number"},
    {"name": "aquira_booked_amount", "label": "Aquira booked amount (if all spots play)", "type": "number", "fieldType": "number"},
    {"name": "aquira_amount_delta", "label": "Aquira amount delta", "type": "number", "fieldType": "number"},
    {
        "name": "aquira_amount_mismatch",
        "label": "Aquira amount mismatch",
        "type": "bool",
        "fieldType": "booleancheckbox",
        "options": BOOL_OPTIONS,
    },
]
REVENUE_PROPS = [
    {"name": "aquira_id", "label": "Aquira ID", "type": "string", "fieldType": "text", "hasUniqueValue": True},
    {"name": "period", "label": "Period", "type": "date", "fieldType": "date"},
    {"name": "amount", "label": "Amount", "type": "number", "fieldType": "number"},
    {"name": "spot_amount", "label": "Spot amount", "type": "number", "fieldType": "number"},
    {"name": "charge_amount", "label": "Charge amount", "type": "number", "fieldType": "number"},
    {
        "name": "source",
        "label": "Source",
        "type": "enumeration",
        "fieldType": "select",
        "options": [
            {"label": "Spot", "value": "spot"},
            {"label": "Charge", "value": "charge"},
            {"label": "Mixed", "value": "mixed"},
        ],
    },
    {"name": "station", "label": "Station", "type": "string", "fieldType": "text"},
    {"name": "station_id", "label": "Station ID", "type": "number", "fieldType": "number"},
    {
        "name": "kind",
        "label": "Kind",
        "type": "enumeration",
        "fieldType": "select",
        "options": [
            {"label": "Proposal", "value": "proposal"},
            {"label": "Booked", "value": "booked"},
        ],
    },
    {"name": "contract_cd", "label": "Contract CD", "type": "string", "fieldType": "text"},
    {"name": "deal_aquira_id", "label": "Deal Aquira ID", "type": "string", "fieldType": "text"},
]
OBJECT_GROUPS = {
    "companies": "companyinformation",
    "company": "companyinformation",
    "contacts": "contactinformation",
    "contact": "contactinformation",
    "deals": "dealinformation",
    "deal": "dealinformation",
}


def _stringify(properties: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in properties.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out


def default_association_type(from_type: str, to_type: str) -> int:
    key = f"{from_type}->{to_type}"
    mapping = {
        "contacts->companies": 1,
        "companies->contacts": 2,
        "deals->companies": 5,
        "companies->deals": 6,
        "companies->companies": 14,
    }
    return mapping.get(key, 1)


class HubSpotClient:
    def __init__(self, access_token: str | None = None):
        settings = get_settings()
        self.access_token = access_token or settings.hubspot_access_token
        self.base_url = "https://api.hubapi.com"
        self.revenue_object_type = "revenue_period"
        self.portal_id: str | None = None
        self._association_cache: dict[str, dict[str, Any]] = {}
        self._prop_names: dict[str, set[str]] = {}

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        response = httpx.request(method, url, headers=self.headers(), timeout=30.0, **kwargs)
        text = response.text
        if response.status_code >= 400:
            raise HubSpotApiError(
                response.status_code,
                text[:800],
                f"HubSpot {method} {path} failed (HTTP {response.status_code}): {text[:240]}",
            )
        if not text:
            return {}
        try:
            return response.json()
        except Exception:
            return {"raw": text}

    def get_owners(self) -> dict[str, Any]:
        if not self.access_token:
            return {"results": []}
        results: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100, "archived": "false"}
            if after:
                params["after"] = after
            response = httpx.get(f"{self.base_url}/crm/v3/owners", headers=self.headers(), params=params, timeout=30.0)
            response.raise_for_status()
            payload = response.json()
            results.extend(payload.get("results") or [])
            after = ((payload.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
            if len(results) >= 1000:
                break
        return {"results": results}

    def get_users(self) -> list[dict[str, Any]]:
        if not self.access_token:
            return []
        results: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if after:
                params["after"] = after
            page = self._request("GET", "/settings/v3/users", params=params)
            results.extend(page.get("results") or [])
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
            if len(results) >= 1000:
                break
        return results

    def get_user_roles(self) -> dict[str, str]:
        if not self.access_token:
            return {}
        page = self._request("GET", "/settings/v3/users/roles")
        roles: dict[str, str] = {}
        for row in page.get("results") or []:
            ident = str(row.get("id") or "")
            if ident:
                roles[ident] = str(row.get("name") or "")
        return roles

    def get_owner_map(self) -> dict[str, str]:
        payload = self.get_owners()
        owner_map: dict[str, str] = {}
        for owner in payload.get("results", []):
            email = (owner.get("email") or "").strip().lower()
            owner_id = owner.get("id") or owner.get("ownerId")
            if email and owner_id:
                owner_map[email] = str(owner_id)
        return owner_map

    def list_owners(self) -> list[dict[str, str]]:
        owners: list[dict[str, str]] = []
        for owner in self.get_owners().get("results", []):
            owner_type = str(owner.get("type") or "PERSON").upper()
            if owner_type and owner_type not in {"PERSON"}:
                continue
            ident = str(owner.get("id") or owner.get("ownerId") or "")
            if not ident:
                continue
            name = (
                f"{owner.get('firstName') or ''} {owner.get('lastName') or ''}".strip()
                or owner.get("name")
                or owner.get("email")
                or ident
            )
            owners.append(
                {
                    "owner_id": ident,
                    "user_id": str(owner.get("userId") or "") or "",
                    "name": name,
                    "email": owner.get("email") or "",
                }
            )
        return owners

    def list_teams(self) -> list[dict[str, Any]]:
        teams: dict[str, dict[str, Any]] = {}
        if self.access_token:
            try:
                payload = self._request("GET", "/settings/v3/users/teams")
                for row in payload.get("results") or []:
                    ident = str(row.get("id") or "")
                    if ident:
                        teams[ident] = {
                            "id": ident,
                            "name": str(row.get("name") or ident),
                            "user_ids": [str(user_id) for user_id in (row.get("userIds") or [])],
                        }
            except Exception:
                pass
            for owner in self.get_owners().get("results") or []:
                for team in owner.get("teams") or []:
                    ident = str(team.get("id") or "")
                    if ident and ident not in teams:
                        teams[ident] = {"id": ident, "name": str(team.get("name") or ident), "user_ids": []}
        return sorted(teams.values(), key=lambda row: str(row.get("name") or "").lower())

    def owner_primary_team_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        if not self.access_token:
            return mapping
        for owner in self.get_owners().get("results") or []:
            ident = str(owner.get("id") or owner.get("ownerId") or "")
            teams = owner.get("teams") or []
            primary = next((team for team in teams if team.get("primary") is True), None)
            if primary is None and len(teams) == 1:
                primary = teams[0]
            if ident and primary and primary.get("id"):
                mapping[ident] = str(primary.get("id"))
        return mapping

    def list_sales_users(self) -> list[dict[str, Any]]:
        """CRM owners joined with portal users so Super Admins can be labeled.

        HubSpot's owner list is anyone who can own a record — often Super Admins.
        Sales-rep mapping prefers non-admin users. Falls back to unlabeled owners
        if the token cannot read settings.users.
        """
        from app.mapping.owners import classify_hubspot_user

        owners = self.list_owners()
        users_by_email: dict[str, dict[str, Any]] = {}
        users_by_id: dict[str, dict[str, Any]] = {}
        roles: dict[str, str] = {}
        try:
            for user in self.get_users():
                email = str(user.get("email") or "").strip().lower()
                ident = str(user.get("id") or "")
                if email:
                    users_by_email[email] = user
                if ident:
                    users_by_id[ident] = user
            roles = self.get_user_roles()
        except Exception:
            users_by_email = {}
            users_by_id = {}
            roles = {}

        rows: list[dict[str, Any]] = []
        for owner in owners:
            email = str(owner.get("email") or "").strip().lower()
            user = users_by_email.get(email) or users_by_id.get(str(owner.get("user_id") or ""))
            super_admin = bool((user or {}).get("superAdmin"))
            role_name = roles.get(str((user or {}).get("roleId") or ""), "")
            if not role_name and super_admin:
                role_name = "Super Admin"
            kind = classify_hubspot_user(role_name, super_admin)
            rows.append(
                {
                    "owner_id": owner["owner_id"],
                    "user_id": owner.get("user_id") or "",
                    "name": owner.get("name") or "",
                    "email": owner.get("email") or "",
                    "role": role_name,
                    "kind": kind,
                    "super_admin": super_admin,
                }
            )
        rows.sort(
            key=lambda row: (
                0 if row["kind"] == "sales" else 1 if row["kind"] == "user" else 2,
                str(row.get("name") or "").lower(),
            )
        )
        return rows

    def get_properties(self, object_type: str) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if after:
                params["after"] = after
            payload = self._request("GET", f"/crm/v3/properties/{object_type}", params=params)
            results.extend(payload.get("results") or [])
            after = ((payload.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return {"results": results}

    def property_group_name(self, object_type: str) -> str:
        if object_type in OBJECT_GROUPS:
            return OBJECT_GROUPS[object_type]
        try:
            payload = self._request("GET", f"/crm/v3/properties/{object_type}/groups")
            for row in payload.get("results") or []:
                name = row.get("name")
                if name and not row.get("archived"):
                    return str(name)
        except Exception as exc:
            logger.debug("HubSpot property groups for %s: %s", object_type, exc)
        return "revenue_period_information"

    def known_property_names(self, object_type: str) -> set[str]:
        cached = self._prop_names.get(object_type)
        if cached is not None:
            return cached
        try:
            names = {str(item.get("name")) for item in (self.get_properties(object_type).get("results") or []) if item.get("name")}
        except Exception as exc:
            logger.warning("Could not list HubSpot %s properties: %s", object_type, exc)
            names = set()
        self._prop_names[object_type] = names
        return names

    def _create_defined_property(self, object_type: str, definition: dict[str, Any]) -> bool:
        body = dict(definition)
        body.setdefault("groupName", self.property_group_name(object_type))
        try:
            self._request("POST", f"/crm/v3/properties/{object_type}", json=body)
            self._prop_names.setdefault(object_type, set()).add(str(definition["name"]))
            return True
        except HubSpotApiError as exc:
            logger.warning(
                "HubSpot property create %s.%s failed (HTTP %s): %s",
                object_type,
                definition.get("name"),
                exc.status,
                (exc.body or "")[:400],
            )
            if body.get("hasUniqueValue"):
                body = dict(body)
                body.pop("hasUniqueValue", None)
                try:
                    self._request("POST", f"/crm/v3/properties/{object_type}", json=body)
                    self._prop_names.setdefault(object_type, set()).add(str(definition["name"]))
                    return True
                except HubSpotApiError:
                    return False
            return exc.status in {409}
        except Exception as exc:
            logger.warning("HubSpot property create %s.%s failed: %s", object_type, definition.get("name"), exc)
            return False

    def prepare_properties(self, object_type: str, properties: dict[str, Any]) -> dict[str, Any]:
        known = set(self.known_property_names(object_type))
        catalog = {item["name"]: item for item in [*COMPANY_PROPS, *CONTACT_PROPS, *DEAL_PROPS, *REVENUE_PROPS]}
        missing = [name for name in properties if name not in known]
        for name in missing:
            definition = catalog.get(name) or {
                "name": name,
                "label": name.replace("_", " ").title(),
                "type": "string",
                "fieldType": "text",
            }
            if self._create_defined_property(object_type, definition):
                known.add(name)
        dropped = [name for name in properties if name not in known]
        if dropped:
            logger.warning("Dropping unknown HubSpot %s properties %s", object_type, dropped)
        return {key: value for key, value in properties.items() if key in known}

    def create_property(
        self,
        object_type: str,
        property_name: str,
        property_type: str = "string",
        field_type: str = "text",
        group_name: str = "coreinformation",
    ) -> dict[str, Any]:
        payload = {
            "name": property_name,
            "label": property_name.replace("_", " ").title(),
            "type": property_type,
            "fieldType": field_type,
            "groupName": group_name,
        }
        response = httpx.post(f"{self.base_url}/crm/v3/properties/{object_type}", headers=self.headers(), json=payload, timeout=30.0)
        response.raise_for_status()
        return response.json()

    def ensure_properties(self, object_type: str, property_names: list[str]) -> list[str]:
        payload = self.get_properties(object_type)
        existing = {item.get("name") for item in payload.get("results", []) if isinstance(item, dict) and item.get("name")}
        created: list[str] = []
        for name in property_names:
            if name in existing:
                continue
            self.create_property(object_type, name)
            created.append(name)
        return created

    def validate_required_properties(self, object_type: str, required_names: list[str]) -> None:
        payload = self.get_properties(object_type)
        existing = {item.get("name") for item in payload.get("results", []) if isinstance(item, dict) and item.get("name")}
        missing = [name for name in required_names if name not in existing]
        if missing:
            raise ValueError(f"Missing required HubSpot {object_type} properties: {', '.join(missing)}")

    def build_company_payload(self, aquira_client: dict[str, Any]) -> dict[str, Any]:
        is_account = bool(_unwrap(aquira_client.get("IsAccount")) is True)
        is_advertiser = bool(_unwrap(aquira_client.get("IsAdvertiser")) is True)
        party_type = "both" if is_account and is_advertiser else "account" if is_account else "advertiser" if is_advertiser else "unknown"
        properties = {
            "name": _unwrap(aquira_client.get("Name")) or "",
            "domain": _unwrap(aquira_client.get("Domain")) or "",
            "phone": _unwrap(aquira_client.get("Phone")) or "",
            "aquira_id": _unwrap(aquira_client.get("ID")),
            "aquira_party_type": party_type,
        }
        return {"properties": {k: v for k, v in properties.items() if v not in (None, "")}}

    def find_company_by_aquira_id(self, aquira_id: str | int | None) -> str | None:
        if aquira_id is None:
            return None
        search_payload = {
            "filterGroups": [{"filters": [{"propertyName": "aquira_id", "operator": "EQ", "value": str(aquira_id)}]}],
            "properties": ["aquira_id"],
            "limit": 1,
        }
        response = httpx.post(f"{self.base_url}/crm/v3/objects/companies/search", headers=self.headers(), json=search_payload, timeout=30.0)
        response.raise_for_status()
        results = response.json().get("results", [])
        for item in results:
            if str(item.get("properties", {}).get("aquira_id", "")) == str(aquira_id):
                return str(item.get("id"))
        return None

    def associate_parent_company(self, child_company_id: str | int, parent_company_id: str | int) -> dict[str, Any]:
        payload = {
            "inputs": [
                {
                    "from": {"id": str(child_company_id)},
                    "to": {"id": str(parent_company_id)},
                    "type": "parent",
                }
            ]
        }
        response = httpx.post(
            f"{self.base_url}/crm/v3/associations/company/company/batch/create",
            headers=self.headers(),
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    def build_contact_payload(self, aquira_contact: dict[str, Any]) -> dict[str, Any]:
        properties = {
            "firstname": _unwrap(aquira_contact.get("FirstName")) or "",
            "lastname": _unwrap(aquira_contact.get("LastName")) or "",
            "email": _unwrap(aquira_contact.get("Email")) or "",
            "phone": _unwrap(aquira_contact.get("Phone")) or "",
            "aquira_id": _unwrap(aquira_contact.get("ID")),
        }
        return {"properties": {k: v for k, v in properties.items() if v not in (None, "")}}

    def find_contact_by_aquira_id(self, aquira_id: str | int | None) -> str | None:
        if aquira_id is None:
            return None
        search_payload = {
            "filterGroups": [{"filters": [{"propertyName": "aquira_id", "operator": "EQ", "value": str(aquira_id)}]}],
            "properties": ["aquira_id"],
            "limit": 1,
        }
        response = httpx.post(f"{self.base_url}/crm/v3/objects/contacts/search", headers=self.headers(), json=search_payload, timeout=30.0)
        response.raise_for_status()
        results = response.json().get("results", [])
        for item in results:
            if str(item.get("properties", {}).get("aquira_id", "")) == str(aquira_id):
                return str(item.get("id"))
        return None

    def upsert_contact(self, aquira_contact: dict[str, Any], associated_company_ids: list[str] | None = None) -> dict[str, Any]:
        payload = self.build_contact_payload(aquira_contact)
        aquira_id = _unwrap(aquira_contact.get("ID"))
        contact_id = self.find_contact_by_aquira_id(aquira_id)
        if contact_id:
            response = httpx.patch(f"{self.base_url}/crm/v3/objects/contacts/{contact_id}", headers=self.headers(), json=payload, timeout=30.0)
        else:
            response = httpx.post(f"{self.base_url}/crm/v3/objects/contacts", headers=self.headers(), json=payload, timeout=30.0)
        response.raise_for_status()
        result = response.json()
        if associated_company_ids:
            for company_id in associated_company_ids:
                assoc_payload = {"inputs": [{"from": {"id": str(result.get("id"))}, "to": {"id": str(company_id)}, "type": "company_to_contact"}]}
                assoc_response = httpx.post(
                    f"{self.base_url}/crm/v3/associations/contact/company/batch/create",
                    headers=self.headers(),
                    json=assoc_payload,
                    timeout=30.0,
                )
                assoc_response.raise_for_status()
        return result

    def build_deal_payload(
        self,
        contract: dict[str, Any],
        account_company_id: str | int | None = None,
        advertiser_company_id: str | int | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        entity = contract.get("Entity", contract)
        aquira_id = _unwrap(entity.get("ID"))
        contract_cd = _unwrap(entity.get("ContractCD")) or _unwrap(entity.get("Name")) or ""
        is_proposal = bool(_unwrap(entity.get("IsProposal")) is True)
        is_contract = bool(_unwrap(entity.get("IsContract")) is True)
        description = str(_unwrap(entity.get("Description")) or "").strip()
        name = f"{contract_cd} — {description or _unwrap(entity.get('Name')) or 'Contract'}"
        properties = {
            "dealname": name,
            "amount": float(_unwrap(entity.get("TotalValue")) or 0),
            "closedate": _unwrap(entity.get("EndDate")) or _unwrap(entity.get("ClosingDate")) or "",
            "pipeline": "default",
            "dealstage": "closedwon" if is_contract else "proposal",
            "aquira_id": aquira_id,
            "aquira_contract_cd": contract_cd,
            "aquira_status": _unwrap(entity.get("Status")) or "",
            "aquira_is_proposal": is_proposal,
            "aquira_is_contract": is_contract,
            "aquira_sign_date": _unwrap(entity.get("SignDate")) or "",
            "aquira_start_date": _unwrap(entity.get("StartDate")) or "",
            "aquira_end_date": _unwrap(entity.get("EndDate")) or "",
            "aquira_account_id": _unwrap(entity.get("AccountID")) or account_company_id,
            "aquira_advertiser_id": _unwrap(entity.get("AdvertiserID")) or advertiser_company_id,
            "hubspot_owner_id": owner_id or "",
        }
        return {"properties": {k: v for k, v in properties.items() if v not in (None, "", 0.0, 0)}}

    def build_revenue_period_payloads(
        self,
        contract: dict[str, Any],
        raw_detail: dict[str, Any] | None = None,
        deal_id: str | None = None,
        company_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        normalized = normalize_spotlines(contract, raw_detail)
        periods = allocate_revenue(normalized)
        payloads: list[dict[str, Any]] = []
        for period in periods:
            properties = {
                "aquira_id": period["aquira_id"],
                "period": period["period"],
                "amount": period["amount"],
                "station": period["station"],
                "station_id": period["station_id"],
                "kind": period["kind"],
                "contract_cd": period["contract_cd"],
                "deal_id": deal_id or "",
                "company_ids": ";".join(company_ids or []),
            }
            payloads.append({"properties": {k: v for k, v in properties.items() if v not in (None, "", 0, 0.0)}})
        return payloads

    def find_deal_by_aquira_id(self, aquira_id: str | int | None) -> str | None:
        if aquira_id is None:
            return None
        search_payload = {
            "filterGroups": [{"filters": [{"propertyName": "aquira_id", "operator": "EQ", "value": str(aquira_id)}]}],
            "properties": ["aquira_id"],
            "limit": 1,
        }
        response = httpx.post(f"{self.base_url}/crm/v3/objects/deals/search", headers=self.headers(), json=search_payload, timeout=30.0)
        response.raise_for_status()
        results = response.json().get("results", [])
        for item in results:
            if str(item.get("properties", {}).get("aquira_id", "")) == str(aquira_id):
                return str(item.get("id"))
        return None

    def associate_deal_to_company(self, deal_id: str | int, company_id: str | int) -> dict[str, Any]:
        payload = {"inputs": [{"from": {"id": str(deal_id)}, "to": {"id": str(company_id)}, "type": "deal_to_company"}]}
        response = httpx.post(
            f"{self.base_url}/crm/v3/associations/deal/company/batch/create",
            headers=self.headers(),
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    def upsert_deal(
        self,
        contract: dict[str, Any],
        account_company_id: str | int | None = None,
        advertiser_company_id: str | int | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self.build_deal_payload(
            contract,
            account_company_id=account_company_id,
            advertiser_company_id=advertiser_company_id,
            owner_id=owner_id,
        )
        aquira_id = _unwrap(contract.get("Entity", {}).get("ID"))
        deal_id = self.find_deal_by_aquira_id(aquira_id)
        if deal_id:
            response = httpx.patch(f"{self.base_url}/crm/v3/objects/deals/{deal_id}", headers=self.headers(), json=payload, timeout=30.0)
        else:
            response = httpx.post(f"{self.base_url}/crm/v3/objects/deals", headers=self.headers(), json=payload, timeout=30.0)
        response.raise_for_status()
        result = response.json()
        for company_id in [company for company in [account_company_id, advertiser_company_id] if company is not None]:
            self.associate_deal_to_company(result.get("id"), company_id)
        return result

    def upsert_company(self, aquira_client: dict[str, Any], parent_company_id: str | int | None = None) -> dict[str, Any]:
        payload = self.build_company_payload(aquira_client)
        aquira_id = _unwrap(aquira_client.get("ID"))
        company_id = self.find_company_by_aquira_id(aquira_id)
        if company_id:
            response = httpx.patch(f"{self.base_url}/crm/v3/objects/companies/{company_id}", headers=self.headers(), json=payload, timeout=30.0)
        else:
            response = httpx.post(f"{self.base_url}/crm/v3/objects/companies", headers=self.headers(), json=payload, timeout=30.0)
        response.raise_for_status()
        result = response.json()
        if parent_company_id and str(parent_company_id) != str(result.get("id")):
            self.associate_parent_company(result.get("id"), parent_company_id)
        return result

    def search_all(
        self,
        object_type: str,
        properties: list[str],
        filter_spec: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            body: dict[str, Any] = {"properties": properties, "limit": 100}
            if after:
                body["after"] = after
            if filter_spec:
                filt = {"propertyName": filter_spec["propertyName"], "operator": filter_spec["operator"]}
                if "value" in filter_spec and filter_spec["value"] is not None:
                    filt["value"] = filter_spec["value"]
                body["filterGroups"] = [{"filters": [filt]}]
            page = self._request("POST", f"/crm/v3/objects/{object_type}/search", json=body)
            for row in page.get("results") or []:
                results.append({"id": str(row.get("id")), "properties": row.get("properties") or {}})
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return results

    def upsert_crm(self, object_type: str, properties: dict[str, Any], existing_id: str | None = None) -> dict[str, Any]:
        properties = self.prepare_properties(object_type, properties)
        payload = {"properties": _stringify(properties)}
        try:
            if existing_id:
                updated = self._request("PATCH", f"/crm/v3/objects/{object_type}/{existing_id}", json=payload)
                return {"id": str(updated.get("id") or existing_id), "properties": updated.get("properties") or payload["properties"]}
            created = self._request("POST", f"/crm/v3/objects/{object_type}", json=payload)
            return {"id": str(created.get("id")), "properties": created.get("properties") or payload["properties"]}
        except HubSpotApiError as err:
            if err.status == 409 and properties.get("aquira_id"):
                found = self.search_all(
                    object_type,
                    list(properties.keys()),
                    {"propertyName": "aquira_id", "operator": "EQ", "value": str(properties["aquira_id"])},
                )
                if found:
                    updated = self._request("PATCH", f"/crm/v3/objects/{object_type}/{found[0]['id']}", json=payload)
                    return {"id": str(updated.get("id") or found[0]["id"]), "properties": updated.get("properties") or payload["properties"]}
            if err.status == 400 and "PROPERTY_DOESNT_EXIST" in (err.body or ""):
                self._prop_names.pop(object_type, None)
                trimmed = self.prepare_properties(object_type, properties)
                if trimmed and trimmed != properties:
                    return self.upsert_crm(object_type, trimmed, existing_id)
            raise

    def archive(self, object_type: str, ident: str) -> None:
        try:
            self._request("DELETE", f"/crm/v3/objects/{object_type}/{ident}")
        except HubSpotApiError as err:
            if err.status == 404:
                return
            raise

    def associate(self, from_type: str, from_id: str, to_type: str, to_id: str, type_id: int | None = None) -> None:
        spec = self._association_spec(from_type, to_type, type_id)
        try:
            self._request("PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}/{to_id}", json=[spec])
        except HubSpotApiError as err:
            if err.status in {400, 409}:
                logger.warning(
                    "HubSpot association %s %s -> %s %s failed (HTTP %s): %s",
                    from_type,
                    from_id,
                    to_type,
                    to_id,
                    err.status,
                    (err.body or "")[:300],
                )
                return
            raise

    def _association_spec(self, from_type: str, to_type: str, type_id: int | None = None) -> dict[str, Any]:
        if type_id:
            category = "HUBSPOT_DEFINED" if from_type in {"contacts", "companies", "deals"} and to_type in {"contacts", "companies", "deals"} else "USER_DEFINED"
            return {"associationCategory": category, "associationTypeId": type_id}
        cache_key = f"{from_type}->{to_type}"
        if cache_key in self._association_cache:
            return self._association_cache[cache_key]
        spec = {
            "associationCategory": "HUBSPOT_DEFINED",
            "associationTypeId": default_association_type(from_type, to_type),
        }
        if from_type not in {"contacts", "companies", "deals"} or to_type not in {"contacts", "companies", "deals"}:
            try:
                payload = self._request("GET", f"/crm/v4/associations/{from_type}/{to_type}/labels")
                row = (payload.get("results") or [{}])[0]
                ident = row.get("typeId") or row.get("id")
                if ident:
                    spec = {
                        "associationCategory": row.get("category") or "USER_DEFINED",
                        "associationTypeId": int(ident),
                    }
            except Exception as exc:
                logger.warning("Could not resolve HubSpot association %s: %s", cache_key, exc)
        self._association_cache[cache_key] = spec
        return spec

    def ensure_crm_schema(self) -> dict[str, Any]:
        created: list[str] = []
        warnings: list[str] = []
        jobs = [("companies", COMPANY_PROPS), ("contacts", CONTACT_PROPS), ("deals", DEAL_PROPS)]
        for object_type, defs in jobs:
            try:
                existing = {item.get("name") for item in (self.get_properties(object_type).get("results") or [])}
            except Exception as exc:
                warnings.append(f"Could not list {object_type} properties: {exc}")
                continue
            for definition in defs:
                if definition["name"] in existing:
                    continue
                try:
                    self._request(
                        "POST",
                        f"/crm/v3/properties/{object_type}",
                        json={
                            **definition,
                            "groupName": OBJECT_GROUPS.get(object_type, "coreinformation"),
                        },
                    )
                    created.append(f"{object_type}.{definition['name']}")
                except HubSpotApiError as exc:
                    logger.warning(
                        "HubSpot property create %s.%s failed (HTTP %s): %s",
                        object_type,
                        definition["name"],
                        exc.status,
                        exc.body[:400],
                    )
                    if exc.status in {400, 409}:
                        continue
                    warnings.append(f"Could not create {object_type}.{definition['name']}: {exc}")
                except Exception as exc:
                    warnings.append(f"Could not create {object_type}.{definition['name']}: {exc}")
        try:
            self.ensure_revenue_object()
            try:
                existing = {item.get("name") for item in (self.get_properties(self.revenue_object_type).get("results") or [])}
            except Exception as exc:
                warnings.append(f"Could not list revenue_period properties: {exc}")
                existing = set()
            for definition in REVENUE_PROPS:
                if definition["name"] in existing:
                    continue
                if self._create_defined_property(self.revenue_object_type, definition):
                    created.append(f"{self.revenue_object_type}.{definition['name']}")
        except Exception as exc:
            warnings.append(f"Revenue period schema unavailable: {exc}")
        return {"created": created, "warnings": warnings}

    def ensure_proposal_stage(self) -> str:
        try:
            payload = self._request("GET", "/crm/v3/pipelines/deals")
            pipeline = (payload.get("results") or [{}])[0]
            stages = pipeline.get("stages") or []
            for stage in stages:
                label = str(stage.get("label") or "").lower()
                ident = str(stage.get("id") or "")
                if label == "proposal" or ident == "proposal":
                    return ident
            for stage in stages:
                if str(stage.get("id") or "").lower() == "contractsent":
                    return str(stage.get("id"))
        except Exception:
            pass
        return "contractsent"

    def ensure_revenue_object(self) -> str:
        try:
            schemas = self._request("GET", "/crm/v3/schemas")
            for schema in schemas.get("results") or []:
                labels = schema.get("labels") or {}
                if schema.get("name") == "revenue_period" or labels.get("singular") == "Revenue Period":
                    ident = schema.get("objectTypeId") or schema.get("name")
                    if ident:
                        self.revenue_object_type = ident
                        return ident
        except Exception:
            pass
        try:
            created = self._request(
                "POST",
                "/crm/v3/schemas",
                json={
                    "name": "revenue_period",
                    "labels": {"singular": "Revenue Period", "plural": "Revenue Periods"},
                    "primaryDisplayProperty": "aquira_id",
                    "requiredProperties": ["aquira_id"],
                    "searchableProperties": ["aquira_id", "contract_cd"],
                    "associatedObjects": ["COMPANY", "DEAL"],
                    "properties": REVENUE_PROPS,
                },
            )
            self.revenue_object_type = created.get("objectTypeId") or created.get("name") or "revenue_period"
            return self.revenue_object_type
        except HubSpotApiError as err:
            if err.status == 409:
                self.revenue_object_type = "revenue_period"
                return self.revenue_object_type
            raise

    def get_record(self, object_type: str, ident: str, properties: list[str]) -> dict[str, Any]:
        row = self._request(
            "GET",
            f"/crm/v3/objects/{object_type}/{ident}",
            params={"properties": ",".join(properties)},
        )
        return {"id": str(row.get("id") or ident), "properties": row.get("properties") or {}}

    def companies_with_aquira(self) -> list[dict[str, Any]]:
        rows = self.search_all(
            "companies",
            ["name", "phone", "domain", "address", "city", "state", "website", "aquira_id", "aquira_party_type", "aquira_version"],
            {"propertyName": "aquira_id", "operator": "HAS_PROPERTY"},
        )
        return [row for row in rows if (row.get("properties") or {}).get("aquira_id")]


    def companies_without_aquira(self) -> list[dict[str, Any]]:
        return self.search_all(
            "companies",
            ["name", "phone", "domain", "address", "city", "state", "website", "aquira_id"],
            {"propertyName": "aquira_id", "operator": "NOT_HAS_PROPERTY"},
        )

    def contacts_with_aquira(self) -> list[dict[str, Any]]:
        rows = self.search_all(
            "contacts",
            ["firstname", "lastname", "email", "phone", "aquira_id", "aquira_entity_type", "aquira_client_id"],
            {"propertyName": "aquira_id", "operator": "HAS_PROPERTY"},
        )
        return [row for row in rows if (row.get("properties") or {}).get("aquira_id")]


    def deals_with_aquira(self) -> list[dict[str, Any]]:
        return self.search_all(
            "deals",
            [
                "dealname",
                "amount",
                "closedate",
                "dealstage",
                "pipeline",
                "hubspot_owner_id",
                "aquira_id",
                "aquira_contract_cd",
                "aquira_status",
                "aquira_is_proposal",
                "aquira_is_contract",
                "aquira_sign_date",
                "aquira_start_date",
                "aquira_end_date",
                "aquira_stations",
                "aquira_account_id",
                "aquira_advertiser_id",
                "aquira_sales_rep",
                "aquira_allocated_amount",
                "aquira_line_total",
                "aquira_spot_total",
                "aquira_charge_total",
                "aquira_booked_amount",
                "aquira_amount_delta",
                "aquira_amount_mismatch",
            ],
            {"propertyName": "aquira_id", "operator": "HAS_PROPERTY"},
        )

    def revenue_with_aquira(self) -> list[dict[str, Any]]:
        try:
            return self.search_all(
                self.revenue_object_type,
                ["aquira_id", "period", "amount", "spot_amount", "charge_amount", "source", "station", "station_id", "kind", "contract_cd", "deal_aquira_id"],
                {"propertyName": "aquira_id", "operator": "HAS_PROPERTY"},
            )
        except HubSpotApiError as err:
            if err.status in {400, 404}:
                return []
            raise

    def projection(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "companies": self.companies_with_aquira(),
            "contacts": self.contacts_with_aquira(),
            "deals": self.deals_with_aquira(),
            "revenue": self.revenue_with_aquira(),
            "owners": self.list_owners(),
        }

    def test_connection(self) -> dict[str, Any]:
        try:
            owners = self.get_owners()
            portal = "connected"
            try:
                info = self._request("GET", "/account-info/v3/details")
                if info.get("portalId"):
                    portal = str(info.get("portalId"))
                    self.portal_id = portal
            except Exception:
                pass
            return {
                "status": "ok",
                "mode": "live",
                "message": "HubSpot token accepted" + ("" if owners.get("results") else " (no owners visible)") + ".",
                "portal": portal,
            }
        except Exception as exc:
            return {"status": "error", "mode": "live", "message": str(exc), "portal": ""}

    @staticmethod
    def verify_signature(payload: bytes, timestamp: str, signature: str, client_secret: str) -> bool:
        if not timestamp or not signature:
            return False
        digest = hmac.new(client_secret.encode(), f"{timestamp}{payload.decode('utf-8', 'ignore')}".encode(), hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def as_existing_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_aquira: dict[str, dict[str, Any]] = {}
    for row in rows:
        properties = row.get("properties") or {}
        aquira_id = str(properties.get("aquira_id") or row.get("aquira_id") or "")
        if not aquira_id:
            continue
        by_aquira[aquira_id] = {
            "hubspotId": str(row.get("id") or row.get("hubspotId") or ""),
            "properties": properties,
            "hash": content_hash(properties),
        }
    return by_aquira
