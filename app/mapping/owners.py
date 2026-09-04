from __future__ import annotations

from typing import Any

ADMIN_ROLE_MARKERS = ("super admin", "superadmin", "app marketplace admin")
SALES_ROLE_MARKERS = (
    "sales",
    "account executive",
    "account manager",
    "advertis",
    "seller",
    "ae ",
    "rep",
)


def _normalize_name(value: Any) -> str:
    return " ".join((value or "").strip().lower().replace("-", " ").split())


def classify_hubspot_user(role_name: str | None = None, super_admin: bool = False) -> str:
    if super_admin:
        return "admin"
    label = (role_name or "").strip().lower()
    if any(marker in label for marker in ADMIN_ROLE_MARKERS):
        return "admin"
    if any(marker in label for marker in SALES_ROLE_MARKERS):
        return "sales"
    return "user"


def _is_admin(person: dict[str, Any]) -> bool:
    if person.get("super_admin") is True:
        return True
    return (person.get("kind") or "") == "admin"


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
    """Map Aquira sales reps to HubSpot users.

    HubSpot "Owner" is the CRM record-owner slot. In small portals that list is
    mostly Super Admins. Auto-suggest therefore prefers sales/non-admin users
    and only maps a Super Admin on an exact email match (same person).
    """
    suggestions: list[dict[str, Any]] = []
    owner_lookup_by_email = {
        (o.get("email") or "").strip().lower(): o for o in hubspot_owners if (o.get("email") or "").strip()
    }
    name_candidates = [o for o in hubspot_owners if not _is_admin(o)]
    owner_lookup_by_name: list[tuple[str, dict[str, Any]]] = []
    for owner in name_candidates:
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
            elif not chosen:
                last = aquira_name.split()[-1] if aquira_name.split() else ""
                if last:
                    last_matches = [
                        owner
                        for normalized_name, owner in owner_lookup_by_name
                        if normalized_name.split() and normalized_name.split()[-1] == last
                    ]
                    if len(last_matches) == 1:
                        chosen = last_matches[0]

        suggestions.append(
            {
                "aquira_user_id": rep.get("id") or rep.get("user_id") or rep.get("ID"),
                "aquira_sales_rep_id": rep.get("sales_rep_id") or rep.get("id") or rep.get("ID"),
                "aquira_name": rep.get("name"),
                "aquira_email": rep.get("email"),
                "hubspot_owner_id": (chosen or {}).get("owner_id") if chosen else None,
                "hubspot_name": (chosen or {}).get("name") if chosen else None,
                "hubspot_email": (chosen or {}).get("email") if chosen else None,
                "hubspot_role": (chosen or {}).get("role") if chosen else None,
                "hubspot_kind": (chosen or {}).get("kind") if chosen else None,
                "enabled": chosen is not None,
                "suggested": chosen is not None,
            }
        )
    return suggestions


def expand_owner_lookup(maps: list[Any], reps: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """Index HubSpot owners by Aquira user id, sales-rep id, and name.

    User/Lookup uses User.ID; contracts use SalesRepID. Both must hit the same mapping.
    """
    lookup: dict[str, str] = {}
    name_to_owner: dict[str, str] = {}
    for row in maps or []:
        enabled = getattr(row, "enabled", None)
        if enabled is None and isinstance(row, dict):
            enabled = row.get("enabled")
        owner_id = getattr(row, "hubspot_owner_id", None) if not isinstance(row, dict) else row.get("hubspot_owner_id")
        user_id = getattr(row, "aquira_user_id", None) if not isinstance(row, dict) else row.get("aquira_user_id")
        sales_rep_id = getattr(row, "aquira_sales_rep_id", None) if not isinstance(row, dict) else row.get("aquira_sales_rep_id")
        name = getattr(row, "aquira_name", None) if not isinstance(row, dict) else row.get("aquira_name")
        if not enabled or not owner_id:
            continue
        owner = str(owner_id)
        for key in (user_id, sales_rep_id):
            if key not in (None, ""):
                lookup[str(key)] = owner
        if name:
            name_to_owner[_normalize_name(name)] = owner
    for rep in reps or []:
        owner = (
            lookup.get(str(rep.get("id") or ""))
            or lookup.get(str(rep.get("user_id") or ""))
            or lookup.get(str(rep.get("sales_rep_id") or ""))
            or name_to_owner.get(_normalize_name(rep.get("name") or rep.get("Name")))
        )
        if not owner:
            continue
        for key in (rep.get("id"), rep.get("user_id"), rep.get("sales_rep_id"), rep.get("ID"), rep.get("SalesRepID")):
            if key not in (None, ""):
                lookup[str(key)] = owner
        name = _normalize_name(rep.get("name") or rep.get("Name"))
        if name:
            name_to_owner[name] = owner
    return lookup
