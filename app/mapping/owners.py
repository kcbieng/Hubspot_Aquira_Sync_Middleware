from __future__ import annotations

from typing import Any

from app.db.models import OwnerMap
from app.db.repo import Repo


def _normalize_name(value: Any) -> str:
    return " ".join((value or "").strip().lower().replace("-", " ").split())


def _name_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.9

    left_tokens = left.split()
    right_tokens = right.split()
    if len(left_tokens) >= 2 and len(right_tokens) >= 2 and left_tokens[-1] == right_tokens[-1]:
        left_first = left_tokens[0]
        right_first = right_tokens[0]
        if left_first == right_first:
            return 0.85
        if left_first.startswith(right_first) or right_first.startswith(left_first):
            return 0.8
    if left_tokens and right_tokens and left_tokens[-1] == right_tokens[-1]:
        return 0.55
    return 0.0


def suggest_owner_map(aquira_reps: list[dict[str, Any]], hubspot_owners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    owner_lookup_by_email = { (o.get("email") or "").strip().lower(): o for o in hubspot_owners if (o.get("email") or "").strip() }
    owner_lookup_by_name: list[tuple[str, dict[str, Any]]] = []
    for owner in hubspot_owners:
        name = (owner.get("name") or "").strip()
        if name:
            owner_lookup_by_name.append((_normalize_name(name), owner))

    for rep in aquira_reps:
        aquira_email = (rep.get("email") or "").strip().lower()
        aquira_name = _normalize_name(rep.get("name"))
        chosen = None

        if aquira_email and aquira_email in owner_lookup_by_email:
            chosen = owner_lookup_by_email[aquira_email]
        elif aquira_name:
            best_match = None
            best_score = 0.0
            for normalized_name, owner in owner_lookup_by_name:
                score = _name_similarity(aquira_name, normalized_name)
                if score > best_score:
                    best_score = score
                    best_match = owner
            if best_score >= 0.75:
                chosen = best_match

        suggestions.append(
            {
                "aquira_user_id": rep.get("id") or rep.get("ID"),
                "aquira_name": rep.get("name"),
                "aquira_email": rep.get("email"),
                "hubspot_owner_id": (chosen or {}).get("owner_id") if chosen else None,
                "hubspot_name": (chosen or {}).get("name") if chosen else None,
                "hubspot_email": (chosen or {}).get("email") if chosen else None,
                "enabled": chosen is not None,
                "suggested": chosen is not None,
            }
        )
    return suggestions


def resolve_owner_id(sales_rep_rows: list[dict[str, Any]], owner_rows: list[OwnerMap]) -> str | None:
    repo = Repo()
    if not owner_rows:
        owner_rows = repo.session.query(OwnerMap).filter(OwnerMap.enabled.is_(True)).all()

    for row in sales_rep_rows:
        sales_rep = row.get("SalesRepID") if isinstance(row, dict) else row
        aquira_id = sales_rep.get("ID") if isinstance(sales_rep, dict) else None
        aquira_name = sales_rep.get("Name") if isinstance(sales_rep, dict) else None
        if aquira_id is None and isinstance(row, dict):
            aquira_id = row.get("id")
        for owner_row in owner_rows:
            if owner_row.enabled is not True:
                continue
            if str(owner_row.aquira_user_id) == str(aquira_id):
                return owner_row.hubspot_owner_id
            if aquira_name and owner_row.aquira_name and owner_row.aquira_name.strip().lower() == aquira_name.strip().lower():
                return owner_row.hubspot_owner_id
    return None
