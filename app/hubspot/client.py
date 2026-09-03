from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import httpx

from app.aquira.contracts import normalize_spotlines
from app.mapping.revenue import allocate_revenue
from app.settings import get_settings


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "Value" in value:
        return value.get("Value")
    return value


class HubSpotClient:
    def __init__(self, access_token: str | None = None):
        settings = get_settings()
        self.access_token = access_token or settings.hubspot_access_token
        self.base_url = "https://api.hubapi.com"

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        return headers

    def get_owners(self) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/crm/v3/owners", headers=self.headers(), params={"limit": 1})
        response.raise_for_status()
        return response.json()

    def get_owner_map(self) -> dict[str, str]:
        payload = self.get_owners()
        owner_map: dict[str, str] = {}
        for owner in payload.get("results", []):
            email = (owner.get("email") or "").strip().lower()
            owner_id = owner.get("ownerId")
            if email and owner_id:
                owner_map[email] = str(owner_id)
        return owner_map

    def get_properties(self, object_type: str) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/crm/v3/properties/{object_type}", headers=self.headers(), params={"limit": 100})
        response.raise_for_status()
        return response.json()

    def create_property(self, object_type: str, property_name: str, property_type: str = "string", field_type: str = "text", group_name: str = "coreinformation") -> dict[str, Any]:
        payload = {
            "name": property_name,
            "label": property_name.replace("_", " ").title(),
            "type": property_type,
            "fieldType": field_type,
            "groupName": group_name,
        }
        response = httpx.post(f"{self.base_url}/crm/v3/properties/{object_type}", headers=self.headers(), json=payload)
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
        response = httpx.post(f"{self.base_url}/crm/v3/objects/companies/search", headers=self.headers(), json=search_payload)
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
        response = httpx.post(f"{self.base_url}/crm/v3/associations/company/company/batch/create", headers=self.headers(), json=payload)
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
        response = httpx.post(f"{self.base_url}/crm/v3/objects/contacts/search", headers=self.headers(), json=search_payload)
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
            response = httpx.patch(f"{self.base_url}/crm/v3/objects/contacts/{contact_id}", headers=self.headers(), json=payload)
        else:
            response = httpx.post(f"{self.base_url}/crm/v3/objects/contacts", headers=self.headers(), json=payload)
        response.raise_for_status()
        result = response.json()
        if associated_company_ids:
            for company_id in associated_company_ids:
                assoc_payload = {"inputs": [{"from": {"id": str(result.get("id"))}, "to": {"id": str(company_id)}, "type": "company_to_contact"}]}
                assoc_response = httpx.post(f"{self.base_url}/crm/v3/associations/contact/company/batch/create", headers=self.headers(), json=assoc_payload)
                assoc_response.raise_for_status()
        return result

    def build_deal_payload(self, contract: dict[str, Any], account_company_id: str | int | None = None, advertiser_company_id: str | int | None = None, owner_id: str | None = None) -> dict[str, Any]:
        entity = contract.get("Entity", contract)
        aquira_id = _unwrap(entity.get("ID"))
        contract_cd = _unwrap(entity.get("ContractCD")) or _unwrap(entity.get("Name")) or ""
        is_proposal = bool(_unwrap(entity.get("IsProposal")) is True)
        is_contract = bool(_unwrap(entity.get("IsContract")) is True)
        name = f"{contract_cd} — {_unwrap(entity.get('Name')) or 'Contract'}"
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

    def build_revenue_period_payloads(self, contract: dict[str, Any], raw_detail: dict[str, Any] | None = None, deal_id: str | None = None, company_ids: list[str] | None = None) -> list[dict[str, Any]]:
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
        response = httpx.post(f"{self.base_url}/crm/v3/objects/deals/search", headers=self.headers(), json=search_payload)
        response.raise_for_status()
        results = response.json().get("results", [])
        for item in results:
            if str(item.get("properties", {}).get("aquira_id", "")) == str(aquira_id):
                return str(item.get("id"))
        return None

    def associate_deal_to_company(self, deal_id: str | int, company_id: str | int) -> dict[str, Any]:
        payload = {"inputs": [{"from": {"id": str(deal_id)}, "to": {"id": str(company_id)}, "type": "deal_to_company"}]}
        response = httpx.post(f"{self.base_url}/crm/v3/associations/deal/company/batch/create", headers=self.headers(), json=payload)
        response.raise_for_status()
        return response.json()

    def upsert_deal(self, contract: dict[str, Any], account_company_id: str | int | None = None, advertiser_company_id: str | int | None = None, owner_id: str | None = None) -> dict[str, Any]:
        payload = self.build_deal_payload(contract, account_company_id=account_company_id, advertiser_company_id=advertiser_company_id, owner_id=owner_id)
        aquira_id = _unwrap(contract.get("Entity", {}).get("ID"))
        deal_id = self.find_deal_by_aquira_id(aquira_id)
        if deal_id:
            response = httpx.patch(f"{self.base_url}/crm/v3/objects/deals/{deal_id}", headers=self.headers(), json=payload)
        else:
            response = httpx.post(f"{self.base_url}/crm/v3/objects/deals", headers=self.headers(), json=payload)
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
            response = httpx.patch(f"{self.base_url}/crm/v3/objects/companies/{company_id}", headers=self.headers(), json=payload)
        else:
            response = httpx.post(f"{self.base_url}/crm/v3/objects/companies", headers=self.headers(), json=payload)
        response.raise_for_status()
        result = response.json()
        if parent_company_id and str(parent_company_id) != str(result.get("id")):
            self.associate_parent_company(result.get("id"), parent_company_id)
        return result

    @staticmethod
    def verify_signature(payload: bytes, timestamp: str, signature: str, client_secret: str) -> bool:
        if not timestamp or not signature:
            return False
        digest = hmac.new(client_secret.encode(), f"{timestamp}{payload.decode('utf-8', 'ignore')}".encode(), hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), signature)
