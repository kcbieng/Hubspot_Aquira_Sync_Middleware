"""Read-only conformance probe for HubQuira.

Run this locally where .env holds the real credentials:

    python scripts/conformance_probe.py aquira
    python scripts/conformance_probe.py hubspot
    python scripts/conformance_probe.py pagination
    python scripts/conformance_probe.py sharding
    python scripts/conformance_probe.py ids
    python scripts/conformance_probe.py tail
    python scripts/conformance_probe.py reads
    python scripts/conformance_probe.py statuses
    python scripts/conformance_probe.py all

It issues ONLY reads: GETs, and POSTs to Load/Search/Lookup/Get endpoints.
Per the Aquira spec, `/Client/Load` and `/Contract/Load` are read-only until
`Edit` is called, and this script never calls Edit, Put, Update, Create, Cancel
or Delete on any resource. The only non-read call is `POST /Session/Post`
(login) and `DELETE /Session/Delete` (logout).

It prints structure and counts, never secrets: no Authorization header, no
cookie value, no password, no token, no property payload. Paste the printed
output back to me as-is.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

TIMEOUT = 45.0
# Sharding/sweep calls must fail fast: a capped-at-45s slow text scan across 42
# buckets looks exactly like a dead script.
SHARD_TIMEOUT = 20.0
# One contract to introspect. Use an id you know exists, or leave as-is.
SAMPLE_CONTRACT_ID = ""
SAMPLE_CLIENT_ID = ""


def _settings() -> Any:
    from app.settings import get_settings

    return get_settings()


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(value.keys())
    if isinstance(value, list):
        return [f"<list len={len(value)}>"] + (_keys(value[0]) if value else [])
    return [f"<{type(value).__name__}>"]


def _count(value: Any, *paths: str) -> int:
    node: Any = value
    for part in paths:
        node = node.get(part) if isinstance(node, dict) else None
    return len(node) if isinstance(node, list) else 0


def _row_ids(payload: Any) -> list[str]:
    rows: Any = None
    if isinstance(payload, dict):
        for key in ("Data", "Items", "Entity"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if isinstance(row, dict):
            ident = row.get("ID") or row.get("Id") or row.get("id")
            out.append(str(ident) if ident is not None else str(sorted(row))[:24])
        else:
            out.append(str(row)[:24])
    return out


# Single-character shard keys: alphanumerics plus the punctuation that actually
# appears in agency/client names.
SHARD_CHARS = list("0123456789abcdefghijklmnopqrstuvwxyz") + [" ", ".", "-", "&", "#", "'"]
TRUNCATION_SENTINEL = 100


def _show(label: str, ok: bool, detail: str) -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {detail}")


# --------------------------------------------------------------------------
# AQUIRA
# --------------------------------------------------------------------------
def probe_aquira() -> None:
    s = _settings()
    base = (s.aquira_base_url or "").rstrip("/")
    if not (base and s.aquira_username and s.aquira_password):
        print("AQUIRA: not configured, skipping")
        return
    client = httpx.Client(base_url=base, timeout=TIMEOUT)
    print(f"\n=== AQUIRA  {base.split('//', 1)[-1].split('/', 1)[0]}  ===")

    # 1. Login. Decides finding B-15: is AppSettings/RoleSettings/SessionTimeOut usable?
    login: dict[str, Any] = {}
    r = client.post(
        "/Session/Post",
        json={"Username": s.aquira_username, "Password": s.aquira_password},
    )
    try:
        login = r.json() if isinstance(r.json(), dict) else {}
    except Exception:
        login = {}
    if not login.get("Success", True) and r.status_code >= 400:
        print(f"  [FAIL] /Session/Post HTTP {r.status_code} — cannot continue, fix creds first")
        return
    _show("login", True, f"HTTP {r.status_code}, top-level keys={_keys(login)[:12]}")
    app_settings = login.get("AppSettings") or (login.get("Entity") or {}).get("AppSettings") or {}
    role_settings = login.get("RoleSettings") or (login.get("Entity") or {}).get("RoleSettings") or {}
    print(f"  [INFO] SessionTimeOut={login.get('SessionTimeOut')!r}")
    print(f"  [INFO] AppSettings keys ({len(app_settings)}): {sorted(app_settings)[:14]}")
    for probe in ("ClientQuickSearchField", "ContractQuickSearchField", "MediaQuickSearchField"):
        print(f"  [INFO] {probe} = {app_settings.get(probe)!r}   (code hardcodes 1 for clients)")
    print(f"  [INFO] RoleSettings keys ({len(role_settings)}): {sorted(role_settings)[:14]}")

    # 2. Enumeration counts. Decides finding B-5: is /Contract/Get really capped at 100?
    calls = {
        "GET /Client/Get": ("get", "/Client/Get", None),
        "POST /Client/Search (term='', QSF=1)": ("post", "/Client/Search", {"SearchTerm": "", "QuickSearchField": 1}),
        "GET /Contract/Get": ("get", "/Contract/Get", None),
        "POST /Contract/Search (term='')": ("post", "/Contract/Search", {"SearchTerm": "", "IncludeActive": True, "IncludeInactive": True}),
        "POST /Contract/Lookup (statuses 0-5)": ("post", "/Contract/Lookup", {"SearchTerm": "", "IncludeStatuses": [0, 1, 2, 3, 4, 5]}),
        "POST /User/Lookup (salesReps)": ("post", "/User/Lookup", {"salesReps": True, "CurrentOnly": True, "SearchTerm": ""}),
    }
    counts: dict[str, Any] = {}
    for label, (verb, path, body) in calls.items():
        try:
            resp = client.request(verb.upper(), path, json=body) if body else client.request(verb.upper(), path)
            payload = resp.json() if resp.content else {}
            n = _count(payload, "Data") or _count(payload, "Entity")
            counts[label] = (resp.status_code, n, payload)
            print(f"  [INFO] {label:44s} HTTP {resp.status_code}  rows={n}")
        except Exception as exc:
            counts[label] = (None, 0, {})
            print(f"  [FAIL] {label:44s} {type(exc).__name__}: {exc}")

    n_get = counts.get("GET /Contract/Get", (None, 0))[1]
    n_search = counts.get("POST /Contract/Search (term='')", (None, 0))[1]
    if n_get == 100 and n_search > 100:
        print("  [VERDICT] /Contract/Get IS truncated at 100 while Search returns more -> "
              "drop /Contract/Get from the union (finding #5/#A6).")
    elif n_get and not n_search:
        print("  [VERDICT] /Contract/Search with empty term returns nothing -> the union "
              "silently relies on the 100-row /Contract/Get. SEVERE.")
    else:
        print(f"  [VERDICT] contract enumeration: Get={n_get} Search={n_search} "
              f"Lookup={counts.get('POST /Contract/Lookup (statuses 0-5)', (None, 0))[1]}")

    # 3. Introspect one contract entity. Decides A1 ({} leak), #16 (flags), A5 (attributes).
    cid = SAMPLE_CONTRACT_ID
    if not cid:
        sample = ((counts.get("POST /Contract/Search (term='')") or (None, 0, {}))[2] or {})
        rows = sample.get("Data") or []
        if rows and isinstance(rows[0], dict):
            cid = str(rows[0].get("ID") or "")
    if cid:
        print(f"\n  --- contract {cid} entity introspection ---")
        try:
            ent = client.post(f"/Contract/Load/{cid}", json={"name": "probe"}).json()
            e = ent.get("Entity") or {}
            print(f"  [INFO] top-level field names ({len(e)}): {sorted(e)[:18]}")
            for flag in ("IsContract", "IsProposal", "Cancelled", "Status", "Version", "EditAllowed", "EditingUser"):
                v = e.get(flag)
                raw = v.get("Value") if isinstance(v, dict) else v
                print(f"  [INFO]   {flag:14s} present={flag in e!s:5s} Value={raw!r} Access={v.get('Access') if isinstance(v, dict) else '-'}")
            attrs = e.get("Attributes")
            if isinstance(attrs, dict):
                rows = attrs.get("Value")
                if isinstance(rows, list):
                    print(f"  [INFO]   Attributes: {len(rows)} row(s)")
                    for row in rows[:6]:
                        rec = row if isinstance(row, dict) else {}
                        val = rec.get("Value")
                        shape = "empty-dict" if val == {} else ("list" if isinstance(val, list) else ("dict" if isinstance(val, dict) else "scalar"))
                        print(f"  [INFO]     name={rec.get('Name')!r} AttrType={rec.get('AttrType')!r} "
                              f"ID={rec.get('ID')!r} ValueShape={shape} Access={rec.get('Access')!r}")
                else:
                    print(f"  [INFO]   Attributes Value shape: {type(rows).__name__}")
            else:
                print(f"  [INFO]   Attributes: absent (key not on contract entity)")
            for line_key in ("MediaLines", "SpotLines", "ChargeLines", "lines"):
                if line_key in e:
                    print(f"  [INFO]   {line_key}: present")
            det = client.post("/Contract/GetContractDetailAnalysis", json={"ID": int(cid) if cid.isdigit() else cid, "id": cid, "RevenueDateType": 0}).json()
            dl = (det.get("Data") or det.get("Entity") or [])
            print(f"  [INFO]   GetContractDetailAnalysis -> rows={len(dl) if isinstance(dl, list) else _keys(dl)}")
            if isinstance(dl, list) and dl and isinstance(dl[0], dict):
                print(f"  [INFO]   detail row keys: {sorted(dl[0])[:16]}")
        except Exception as exc:
            print(f"  [FAIL] contract introspection: {type(exc).__name__}: {exc}")

    # 4. Introspect one client. Decides A1/A3/A5 (contacts + addresses + website nesting).
    kid = SAMPLE_CLIENT_ID
    if not kid:
        sample = ((counts.get("GET /Client/Get") or (None, 0, {}))[2] or {})
        rows = sample.get("Data") or []
        if rows and isinstance(rows[0], dict):
            kid = str(rows[0].get("ID") or "")
    if kid:
        print(f"\n  --- client {kid} entity introspection ---")
        try:
            ent = client.post(f"/Client/Load/{kid}").json()
            e = ent.get("Entity") or {}
            print(f"  [INFO] top-level field names ({len(e)}): {sorted(e)[:22]}")
            for key in ("Email", "Phone", "Website", "Domain", "Contacts", "ClientContacts", "Addresses", "ContactDetails", "PhysicalAddress", "IsAccount", "IsAdvertiser", "AccountID", "Version", "Attributes"):
                print(f"  [INFO]   {key:16s} present={key in e}")
            cd = e.get("ContactDetails")
            if isinstance(cd, dict):
                inner = cd.get("Value")
                print(f"  [INFO]   ContactDetails.Value keys: {sorted(inner)[:14] if isinstance(inner, dict) else _keys(inner)}")
            ad = e.get("Addresses")
            if isinstance(ad, dict):
                inner = ad.get("Value")
                print(f"  [INFO]   Addresses.Value keys: {sorted(inner) if isinstance(inner, dict) else _keys(inner)}")
                phys = (inner or {}).get("Physical") if isinstance(inner, dict) else None
                if isinstance(phys, dict):
                    print(f"  [INFO]   Physical keys: {sorted(phys)[:12]}")
                    addr = phys.get("Address")
                    print(f"  [INFO]   Address raw = {json.dumps(addr)[:150] if addr is not None else 'absent'}")
                    print(f"  [INFO]   Address.Value type = {type((addr or {}).get('Value') if isinstance(addr, dict) else addr).__name__}  (str() of a dict is where '{{}}' leaks)")
            lc = client.post("/Client/LookupContacts", json={"id": int(kid) if kid.isdigit() else kid}).json()
            rows = lc.get("Data") or []
            print(f"  [INFO]   LookupContacts rows={len(rows)}")
            if rows and isinstance(rows[0], dict):
                first = rows[0].get("Value") if isinstance(rows[0].get("Value"), dict) else rows[0]
                print(f"  [INFO]   contact keys: {sorted(first)[:20]}")
                for key in ("Email", "EmailAddress", "Phone", "BusinessPhone1", "PersonalDirectDialPhone", "FirstName", "LastName", "Name"):
                    print(f"  [INFO]     {key:24s} present={key in first}")
        except Exception as exc:
            print(f"  [FAIL] client introspection: {type(exc).__name__}: {exc}")

    # 5. Error-code table. Decides whether -16/-7 handling can be made data-driven.
    try:
        codes = client.get("/AquiraAPI/ErrorCodes").json()
        n = len(codes.get("Data") or codes.get("Entity") or {})
        _show("ErrorCodes", n > 0, f"entries={n} (code hardcodes -7 and never checks -16)")
    except Exception as exc:
        print(f"  [FAIL] ErrorCodes: {exc}")
    try:
        gs = client.get("/GlobalSettings/Get").json()
        u = ((gs.get("Entity") or {}).get("User") or {})
        print(f"  [INFO] GlobalSettings User.PasswordMinimumLength={u.get('PasswordMinimumLength')!r} "
              f"PasswordExpiryReminderDays={u.get('PasswordExpiryReminderDays')!r}  (service-account expiry risk)")
    except Exception as exc:
        print(f"  [FAIL] GlobalSettings: {exc}")

    try:
        client.delete("/Session/Delete")
    except Exception:
        pass
    client.close()


# --------------------------------------------------------------------------
# HUBSPOT
# --------------------------------------------------------------------------
def probe_hubspot() -> None:
    s = _settings()
    token = s.hubspot_access_token
    if not token:
        print("HUBSPOT: not configured, skipping")
        return
    H = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    c = httpx.Client(base_url="https://api.hubapi.com", headers=H, timeout=TIMEOUT)
    print("\n=== HUBSPOT ===")

    def get(path: str, **params: Any) -> tuple[int | None, Any]:
        try:
            r = c.get(path, params=params or None)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {}
        except Exception as exc:
            return None, {"error": f"{type(exc).__name__}: {exc}"}

    # 1. THE decider for finding #3: which schemas path works?
    print("\n  --- custom object schemas (finding #3) ---")
    for path in ("/crm/v3/schemas", "/crm-object-schemas/v3/schemas", "/crm-object-schemas/2026-09/schemas"):
        status, payload = get(path, limit=100)
        rows = payload.get("results") if isinstance(payload, dict) else None
        print(f"  [INFO] GET {path:46s} -> HTTP {status}  schemas={len(rows) if isinstance(rows, list) else '-'}")
        if isinstance(rows, list):
            for row in rows:
                if "revenue" in str(row.get("name", "")).lower():
                    print(f"  [VERDICT] found schema name={row.get('name')!r} objectTypeId={row.get('objectTypeId')!r} "
                          f"fullyQualifiedName={row.get('fullyQualifiedName')!r} id={row.get('id')!r}")
                    props = row.get("properties") or {}
                    for pname in ("aquira_id", "amount", "period"):
                        p = props.get(pname) if isinstance(props, dict) else None
                        if isinstance(p, dict):
                            print(f"  [INFO]   property {pname}: hasUniqueValue={p.get('hasUniqueValue')!r}")

    # 2. Enterprise gating + real caps.
    print("\n  --- limits / gating (finding #3, B-low) ---")
    for path in ("/crm/limits/2026-09/custom-object-types", "/crm/limits/2026-09/records",
                 "/crm/limits/2026-09/custom-properties", "/crm/v3/limits/total"):
        status, payload = get(path)
        keys = sorted(payload)[:8] if isinstance(payload, dict) else []
        print(f"  [INFO] GET {path:46s} -> HTTP {status}  {json.dumps(payload)[:220]}")

    # 3. Association type table. Settles finding #9 and my 13/14 retraction.
    print("\n  --- association labels (finding #9) ---")
    for pair in ("company/company", "deal/company", "contact/company"):
        status, payload = get(f"/crm/v4/associations/{pair}/labels", limit=100)
        rows = payload.get("results") if isinstance(payload, dict) else None
        print(f"  [INFO] {pair} -> HTTP {status}")
        for row in (rows or [])[:14]:
            print(f"  [INFO]   typeId={row.get('typeId') or row.get('id')!r:12} label={row.get('label')!r} "
                  f"name={row.get('name')!r} category={row.get('category')!r}")
        if isinstance(rows, list) and not rows:
            print("  [INFO]   (empty results)")

    # 4. Does any collection exceed the 10k search cap? (finding #2)
    print("\n  --- search cap exposure (finding #2) ---")
    for obj in ("companies", "contacts", "deals", "revenue_period"):
        body = {"filterGroups": [{"filters": [{"propertyName": "aquira_id", "operator": "HAS_PROPERTY"}]}],
                "properties": ["aquira_id"], "limit": 200, "total": True}
        try:
            r = c.post(f"/crm/v3/objects/{obj}/search", json=body)
            payload = r.json() if r.content else {}
            print(f"  [INFO] search {obj:15s} (limit 200) -> HTTP {r.status_code} "
                  f"results={len(payload.get('results') or [])} total={payload.get('total')!r} "
                  f"next={((payload.get('paging') or {}).get('next') or {}).get('after')!r}")
        except Exception as exc:
            print(f"  [FAIL] search {obj}: {exc}")
        status, payload = get(f"/crm/v3/objects/{obj}", limit=100)
        after = ((payload.get("paging") or {}).get("next") or {}).get("after") if isinstance(payload, dict) else None
        print(f"  [INFO] list   {obj:15s} -> HTTP {status} results={len((payload or {}).get('results') or [])} next.after={after!r}")

    # 5. Owners: how many are archived (finding B8)?
    print("\n  --- owners (finding B8) ---")
    for archived in ("false", "true"):
        status, payload = get("/crm/v3/owners", limit=100, archived=archived)
        rows = payload.get("results") if isinstance(payload, dict) else []
        print(f"  [INFO] archived={archived:5s} -> HTTP {status} owners={len(rows or [])}")
        if rows:
            r0 = rows[0]
            print(f"  [INFO]   sample keys: {sorted(r0)[:10]} teams={len(r0.get('teams') or [])}")

    # 6. Webhook subscriptions: one-per-property requirement (finding #11).
    print("\n  --- webhook subscriptions (finding #11) ---")
    for path in ("/webhooks/v3/PORTAL_OR_APP_ID/subscriptions",):
        print(f"  [INFO] skipped (needs appId). In Postman GET /webhooks/v3/{'{appId}'}/subscriptions "
              "and count how many propertyChange subs exist and their propertyName.")

    # 7. Are identity properties actually present + searchable?
    print("\n  --- property health ---")
    expected = {
        "companies": ("aquira_id", "aquira_client_cd", "aquira_party_type", "aquira_version", "aquira_hubspot_team"),
        "contacts": ("aquira_id", "aquira_entity_type", "aquira_client_id", "aquira_hubspot_team"),
        "deals": ("aquira_id", "aquira_contract_cd", "aquira_status", "aquira_allocated_amount",
                  "aquira_amount_mismatch", "aquira_hubspot_team"),
    }
    for obj, core in expected.items():
        status, payload = get(f"/crm/v3/properties/{obj}", limit=200)
        rows = payload.get("results") if isinstance(payload, dict) else []
        names = {str(r.get("name")) for r in (rows or [])}
        mine = sorted(n for n in names if n.startswith("aquira_"))
        missing = [n for n in core if n not in names]
        print(f"  [INFO] {obj:10s} -> HTTP {status} total_props={len(rows or [])} aquira_props={len(mine)} "
              f"missing_core={missing or 'none'}  (custom props count against the 1,000/object cap)")
    c.close()


# --------------------------------------------------------------------------
# RAW SWAGGER SPEC
# --------------------------------------------------------------------------
# Aquira_Swagger_docs.txt is a *rendered* Swagger-UI text export, and several
# operations (/Client/Get, /Contract/Get, /Client/Create) show no Parameters
# block at all, so the rendered file cannot prove whether paging exists. The raw
# JSON can. Swagger 2.0 declares paging as ordinary query/body parameters and
# exposes vendor extras as x- extensions, so both are scanned here.

PAGING_WORDS = (
    "page", "pagenumber", "pagesize", "limit", "offset", "skip", "top", "take",
    "count", "totalcount", "rowcount", "cursor", "continuation",
    "startindex", "rows", "max", "after", "before", "since", "modifiedsince",
    "lastmodified", "range", "batch",
)

ENUM_OPS = (
    "/Client/Get", "/Client/Search", "/Client/AdvancedSearch", "/Client/Lookup",
    "/Client/SearchByID", "/Contract/Get", "/Contract/Search",
    "/Contract/AdvancedSearch", "/Contract/Lookup", "/Contract/SearchByID",
    "/User/Lookup", "/Report/RunAdvancedSearchReport",
    "/Report/LoadGeneratedReport",
)


def probe_swagger() -> None:
    s = _settings()
    base = (s.aquira_base_url or "").rstrip("/")
    if not base:
        print("SWAGGER: no AQUIRA_BASE_URL, skipping")
        return
    client = httpx.Client(base_url=base, timeout=TIMEOUT)
    print("\n=== RAW SWAGGER SPEC ===")
    spec = None
    for path in ("/swagger/docs/v1", "/swagger/v1/swagger.json", "/swagger/docs"):
        try:
            r = client.get(path)
            print(f"  [INFO] GET {path:34s} -> HTTP {r.status_code} bytes={len(r.content)}")
            if r.status_code == 200 and r.content:
                try:
                    spec = r.json()
                    print(f"  [INFO] parsed JSON from {path}")
                    break
                except Exception as exc:
                    print(f"  [FAIL] not JSON: {exc}")
        except Exception as exc:
            print(f"  [FAIL] GET {path}: {type(exc).__name__}")
    if not isinstance(spec, dict) and s.aquira_username and s.aquira_password:
        print("  [INFO] retrying with a logged-in session")
        client.post("/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password})
        for path in ("/swagger/docs/v1", "/swagger/v1/swagger.json"):
            try:
                resp = client.get(path)
                if resp.status_code == 200:
                    spec = resp.json()
                    print(f"  [INFO] parsed JSON from {path} (authenticated)")
                    break
            except Exception:
                pass
    if not isinstance(spec, dict):
        print("  [VERDICT] could not fetch raw swagger JSON. Open "
              f"{base}/swagger/docs/v1 in a browser, save the file, then point the probe at it.")
        client.close()
        return

    print(f"  [INFO] swagger version={spec.get('swagger') or spec.get('openapi')!r} "
          f"basePath={spec.get('basePath')!r} host={spec.get('host')!r}")
    paths = spec.get("paths") or {}
    print(f"  [INFO] paths={len(paths)}")

    all_params: dict[str, int] = {}
    ext_keys: set[str] = set()
    paging_hits: dict[str, set[str]] = {}
    for ppath, item in paths.items():
        if not isinstance(item, dict):
            continue
        for verb, op in item.items():
            if str(verb).startswith("x-"):
                ext_keys.add(f"{ppath} {verb}")
            if not isinstance(op, dict) or verb in {"parameters", "$ref"}:
                continue
            for key in op:
                if str(key).startswith("x-"):
                    ext_keys.add(f"{verb.upper()} {ppath} {key}")
            for prm in op.get("parameters") or []:
                name = str(prm.get("name") or prm.get("$ref") or "?")
                all_params[name] = all_params.get(name, 0) + 1
                if any(word in name.lower() for word in PAGING_WORDS):
                    paging_hits.setdefault(name, set()).add(f"{verb.upper()} {ppath}")
    print(f"\n  [INFO] distinct parameter names declared anywhere: {len(all_params)}")
    if ext_keys:
        print(f"  [INFO] x- specification extensions ({len(ext_keys)}): {sorted(ext_keys)[:15]}")
    else:
        print("  [INFO] no x- specification extensions declared")
    for hit in sorted(paging_hits):
        print(f"  [FLAG] paging-shaped param {hit!r} on {sorted(paging_hits[hit])[:3]}")
    if not paging_hits:
        print("  [VERDICT] NO paging-shaped parameter is declared anywhere in the raw spec "
              "-> the 100-row cap cannot be worked around with a page/limit argument.")

    print("\n  --- enumeration operations, declared parameters ---")
    for ppath in ENUM_OPS:
        item = paths.get(ppath)
        if not isinstance(item, dict):
            print(f"  [INFO] {ppath}: NOT PRESENT in spec")
            continue
        for verb, op in item.items():
            if not isinstance(op, dict) or verb in {"parameters", "$ref"}:
                continue
            prm = op.get("parameters") or []
            names = [f"{p.get('in')}:{p.get('name')}" for p in prm]
            body = [p for p in prm if p.get("in") == "body"]
            ref = str((body[0].get("schema") or {}).get("$ref") or "") if body else ""
            print(f"  [INFO] {verb.upper():6s} {ppath:42s} params={names or 'NONE'} body={ref or '-'}")

    defs = spec.get("definitions") or {}
    print(f"\n  --- response models ({len(defs)} definitions) ---")
    hits = 0
    for dname, dbody in sorted(defs.items()):
        for pname in ((dbody or {}).get("properties") or {}):
            if any(word in str(pname).lower() for word in PAGING_WORDS):
                print(f"  [FLAG] {dname}.{pname}")
                hits += 1
    if not hits:
        print("  [VERDICT] no totalCount/hasMore/cursor field on ANY response model -> "
              "truncation is undetectable from the payload; the rows==100 sentinel and "
              "count-vs-previous-run are the only available signals.")

    print("\n  --- advanced-search request schemas (filter-sharding candidate) ---")
    seen: set[str] = set()
    for ppath in ("/Client/AdvancedSearch", "/Contract/AdvancedSearch", "/Filter/LookupFilterFields"):
        for verb, op in (paths.get(ppath) or {}).items():
            if not isinstance(op, dict):
                continue
            for prm in op.get("parameters") or []:
                ref = str((prm.get("schema") or {}).get("$ref") or "").lstrip("#/definitions/")
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                props = ((defs.get(ref) or {}).get("properties") or {})
                print(f"  [INFO] {ppath} -> {ref}: {sorted(props)}")
                for pname, pbody in props.items():
                    sub = str((pbody or {}).get("$ref") or (pbody or {}).get("items", {}).get("$ref") or "")
                    if sub:
                        sname = sub.lstrip("#/definitions/")
                        print(f"  [INFO]   {pname} -> {sname}: "
                              f"{sorted(((defs.get(sname) or {}).get('properties') or {}))}")

    # RunReportArgs is the bulk-extraction escape hatch: if it carries a filter or
    # date range, one report can replace the whole per-contract sweep.
    print("\n  --- report request models ---")
    for dname in ("Aquira_APIPOCO.Report.RunReportArgs", "Aquira_APIPOCO.Report.ReportRequest"):
        props = ((defs.get(dname) or {}).get("properties") or {})
        if props:
            print(f"  [INFO] {dname}: {sorted(props)}")
    for dname, dbody in sorted(defs.items()):
        if "RunReport" in dname or dname.endswith("ReportArgs"):
            print(f"  [INFO] {dname}: {sorted(((dbody or {}).get('properties') or {}))}")
    try:
        client.delete("/Session/Delete")
    except Exception:
        pass
    client.close()


def probe_filter_fields() -> None:
    """Which predicates can AdvancedSearch actually filter on? This decides whether
    the 100-row cap can be worked around by sharding."""
    s = _settings()
    base = (s.aquira_base_url or "").rstrip("/")
    if not (base and s.aquira_username and s.aquira_password):
        print("FILTERS: aquira credentials required")
        return
    client = httpx.Client(base_url=base, timeout=TIMEOUT)
    print("\n=== ADVANCED-SEARCH FILTER FIELDS ===")
    login = client.post("/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password}).json()
    if not login.get("Success", True):
        print("  [FAIL] login refused, cannot query filter fields")
        client.close()
        return
    for area in ("", "Clients", "Client", "Contracts", "Contract", "Proposals", "Agreements", "Media"):
        try:
            r = client.post("/Filter/LookupFilterFields", json={"FilterArea": area, "SearchTerm": ""})
            rows = (r.json() or {}).get("Data") or []
        except Exception as exc:
            print(f"  [FAIL] FilterArea={area!r}: {type(exc).__name__}")
            continue
        print(f"  [INFO] FilterArea={area!r:14s} -> HTTP {r.status_code} fields={len(rows)}")
        for row in rows[:40]:
            if not isinstance(row, dict):
                continue
            name = row.get("Name") or row.get("ShortName")
            ops = row.get("AvailableFilterOperators") or []
            print(f"  [INFO]   {str(name):32s} type={row.get('FieldType')!r:6} "
                  f"isLookup={row.get('IsLookup')!r:6} short={row.get('ShortName')!r} ops={ops}")
        if len(rows) >= 2:
            print(f"  [VERDICT] FilterArea={area!r} is valid and offers {len(rows)} predicates "
                  "-> sharding the 100-row cap is feasible.")
    try:
        client.delete("/Session/Delete")
    except Exception:
        pass
    client.close()


def probe_pagination() -> None:
    """Does undocumented paging work even though the spec never declares it?

    Swagger 2.0 on ASP.NET Web API only emits parameters it can describe. A value
    read off HttpContext.Request.QueryString, or a simple type bound [FromUri]
    next to a [FromBody] complex type, is invisible to the spec but fully
    functional — so "params=NONE" is not proof of absence. Everything here is a
    read. HTTP status is meaningless as evidence because these endpoints answer
    200 to anything, so the test is whether the returned ID SET changes.
    /Forecast/Search is the positive control: it is the one request model in the
    spec that really does declare OffSet.
    """
    s = _settings()
    base = (s.aquira_base_url or "").rstrip("/")
    if not (base and s.aquira_username and s.aquira_password):
        print("PAGINATION: aquira credentials required")
        return
    client = httpx.Client(base_url=base, timeout=TIMEOUT)
    login = client.post(
        "/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password}
    ).json()
    if not login.get("Success", True):
        print("  [FAIL] login refused")
        client.close()
        return
    print("\n=== PAGINATION PROBE (reads only) ===")

    single = [
        ("page", "2"), ("page", "3"), ("pageNumber", "2"), ("pageNo", "2"), ("p", "2"),
        ("limit", "10"), ("per_page", "10"), ("pageSize", "10"), ("size", "10"),
        ("count", "10"), ("top", "10"), ("take", "10"), ("max", "10"), ("rows", "10"),
        ("maxRecords", "10"), ("maxresults", "10"),
        ("skip", "10"), ("offset", "10"), ("startIndex", "10"), ("recordOffset", "10"),
        ("$top", "10"), ("$skip", "10"), ("$page", "2"),
    ]
    combos = [
        {"page": "1", "limit": "50"}, {"page": "2", "limit": "50"}, {"page": "2", "limit": "10"},
        {"limit": "50", "offset": "50"}, {"skip": "50", "take": "50"},
        {"pageSize": "50", "pageNumber": "2"}, {"per_page": "50", "page": "2"},
        {"$top": "50", "$skip": "50"}, {"limit": "1000"}, {"pageSize": "500"},
        {"OffSet": "50", "Limit": "50"}, {"offset": "100", "limit": "100"},
    ]

    def run(label: str, verb: str, path: str, body: Any, params: dict[str, str]) -> dict[str, Any]:
        try:
            r = client.request(verb, path, json=body, params=params or None)
            try:
                payload = r.json()
            except Exception:
                payload = {}
            ids = _row_ids(payload)
            return {
                "label": label, "status": r.status_code, "rows": len(ids), "ids": ids,
                "first": ids[0] if ids else "", "last": ids[-1] if ids else "",
                "headers": {k: v for k, v in r.headers.items() if any(
                    t in k.lower() for t in ("total", "page", "range", "count", "link"))},
            }
        except Exception as exc:
            return {"label": label, "status": None, "rows": -1, "ids": [], "first": "",
                    "last": "", "headers": {}, "error": type(exc).__name__}

    def report(baseline: dict[str, Any], results: list[dict[str, Any]]) -> None:
        base_ids = set(baseline["ids"])
        print(f"\n  baseline {baseline['label']}: rows={baseline['rows']} "
              f"first={baseline['first']!r} last={baseline['last']!r}")
        if baseline["headers"]:
            print(f"    paging-ish response headers: {baseline['headers']}")
        else:
            print("    no X-Total-Count / Content-Range / Link headers present")
        changed = 0
        for res in results:
            new = [i for i in res["ids"] if i not in base_ids]
            if new or (res["rows"] >= 0 and res["rows"] != baseline["rows"]):
                changed += 1
                print(f"  [DIFFERENT] {res['label']:46s} HTTP {res['status']} rows={res['rows']} "
                      f"new_ids={len(new)} first={res['first']!r} last={res['last']!r} "
                      f"{res['headers'] or ''}")
        if not changed:
            print("  [IGNORED] every parameter set returned the identical row set "
                  "-> the server is not reading these names")
        else:
            print(f"  [VERDICT] {changed} of {len(results)} parameter sets changed the result "
                  "-> PAGING MAY EXIST. Capture the [DIFFERENT] lines and I will build on it.")

    for path in ("/Client/Get", "/Contract/Get"):
        baseline = run(f"GET {path}", "GET", path, None, {})
        if baseline["rows"] < 0:
            print(f"  [FAIL] GET {path} unreachable: {baseline.get('error')}")
            continue
        report(baseline, [run(f"GET {path}?{k}={v}", "GET", path, None, {k: v}) for k, v in single]
               + [run(f"GET {path}?{sorted(c)}", "GET", path, None, c) for c in combos])

    for path, body in (
        ("/Client/Search", {"SearchTerm": "", "QuickSearchField": 8}),
        ("/Contract/Search", {"SearchTerm": "", "IncludeActive": True, "IncludeInactive": True}),
        ("/Client/AdvancedSearch", {"SearchTerm": ""}),
        ("/Contract/AdvancedSearch", {"SearchTerm": ""}),
    ):
        baseline = run(f"POST {path}", "POST", path, body, {})
        if baseline["rows"] < 0:
            print(f"  [FAIL] POST {path} unreachable: {baseline.get('error')}")
            continue
        if baseline["rows"] == 0:
            print(f"  [INCONCLUSIVE] POST {path}: empty-query baseline returns 0 rows, so a "
                  "0-row param matrix proves nothing about paging on this endpoint.")
        results = [run(f"POST {path}?{k}={v}", "POST", path, body, {k: v}) for k, v in single]
        results += [
            run(f"POST {path} body+{k}={v}", "POST", path,
                {**body, k: int(v) if v.lstrip("$").isdigit() else v}, {})
            for k, v in single
        ]
        results += [
            run(f"POST {path}?{sorted(c)}", "POST", path, body,
                {kk: str(vv) for kk, vv in c.items()})
            for c in combos
        ]
        results += [
            run(f"POST {path} body{sorted(c)}", "POST", path,
                {**body, **{kk: int(vv) if str(vv).lstrip("$").isdigit() else vv
                            for kk, vv in c.items()}}, {})
            for c in combos
        ]
        report(baseline, results)

    print("\n  --- control: /Forecast/Search, the only request model whose spec declares OffSet ---")
    ctrl_bodies = ({}, {"OffSet": 0}, {"OffSet": 1}, {"OffSet": 2}, {"OffSet": 50},
                   {"Offset": 50, "Limit": 10}, {"OffSet": 50, "PageSize": 10},
                   {"OffSet": 0, "Limit": 10}, {"OffSet": 10, "Limit": 10})
    ctrl = []
    for body in ctrl_bodies:
        res = run(f"POST /Forecast/Search {body}", "POST", "/Forecast/Search", body, {})
        ctrl.append(res)
        print(f"  [INFO] {str(body):40s} HTTP {res['status']} rows={res['rows']} "
              f"first={res['first']!r} last={res['last']!r}")
    crash_at = [b for b, r in zip(ctrl_bodies, ctrl) if r["status"] == 500]
    ok_at = [b for b, r in zip(ctrl_bodies, ctrl) if r["status"] == 200]
    if crash_at and any("OffSet" in str(b) or "Offset" in str(b) for b in crash_at):
        print(f"  [VERDICT] control: OffSet IS parsed by the server — HTTP 500 only on {str(crash_at[0])} "
              f"while {str(ok_at[0] if ok_at else '{}')} is 200. An out-of-range offset crashes it, so the "
              "harness CAN surface paging where it exists; it simply does not exist on the enumeration "
              "endpoints above.")
    else:
        print("  [INFO] control showed no OffSet sensitivity; negative enumeration results are "
              "weaker because the harness could not prove a positive.")

    try:
        client.delete("/Session/Delete")
    except Exception:
        pass
    client.close()


# --------------------------------------------------------------------------
# SHARDING: the only workarounds left now that paging is proven absent
# --------------------------------------------------------------------------
def probe_sharding() -> None:
    """If the server caps every read at 100 rows and honors no paging parameter,
    complete enumeration is only possible by partitioning the query space so each
    bucket returns < 100 rows. A first live run showed /Client/Search (QSF=1)
    answers 100 rows for the empty term and 0 for EVERY one-char term — the term
    is read but never matches at length 1 — so a QuickSearchField semantics matrix
    runs first to find fields where terms match at all. A bucket at exactly 100 is
    indistinguishable from truncation, so the probe fails loudly on hot buckets
    instead of reporting a false union. Progress prints per bucket: a slow server
    must not look like a dead script."""
    s = _settings()
    base = (s.aquira_base_url or "").rstrip("/")
    if not (base and s.aquira_username and s.aquira_password):
        print("SHARDING: aquira credentials required")
        return
    client = httpx.Client(base_url=base, timeout=SHARD_TIMEOUT)
    login = client.post(
        "/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password}
    ).json()
    if not login.get("Success", True):
        print("  [FAIL] login refused")
        client.close()
        return
    print("\n=== SHARDING PROBE (reads only) ===")

    def fetch(path: str, body: dict) -> tuple[int | None, list[str]]:
        try:
            r = client.request("POST", path, json=body)
            try:
                return r.status_code, _row_ids(r.json())
            except Exception:
                return r.status_code, []
        except Exception:
            return None, []

    # 1. Semantics matrix: which QuickSearchField (if any) answers non-empty terms?
    #    The login payload told us the tenant's own UI setting for ClientQuickSearchField.
    app_settings = login.get("AppSettings") or (login.get("Entity") or {}).get("AppSettings") or {}
    ui_qsf = app_settings.get("ClientQuickSearchField")
    print(f"\n  --- /Client/Search semantics (tenant UI default QSF={ui_qsf!r}) ---")
    terms = ["a", "s", "u", "un", "inc", "llc", "1", "12", "45", "245"]
    live_qsf: list[int] = []
    for qsf in range(0, 9):
        counts = []
        for t in terms:
            _, rows = fetch("/Client/Search", {"SearchTerm": t, "QuickSearchField": qsf})
            counts.append(len(rows))
        if any(counts):
            live_qsf.append(qsf)
        print(f"  [INFO] QSF={qsf}: " + " ".join(f"{t}:{n}" for t, n in zip(terms, counts))
              + ("   <- matches non-empty terms" if any(counts) else ""))
    if not live_qsf:
        print("  [VERDICT] No QuickSearchField 0-8 answers ANY non-empty term -> /Client/Search "
              "sharding is dead; the ID sweep (probe_id_sweep) is the only complete spine for clients.")
    else:
        print(f"  [INFO] usable QSF values: {live_qsf} — prefix sharding is attempted on those below "
              "only if ONE-CHAR terms match (a 2-char key needs 36^2 calls and is not probed here).")

    def shard_by_term(path: str, body_of, label: str) -> None:
        base_status, base_ids = fetch(path, body_of(""))
        ids: set[str] = set(base_ids)
        print(f"\n  {label}: baseline(empty term) HTTP {base_status} rows={len(base_ids)}")
        print("  one-char buckets: ", end="", flush=True)

        def bucket(prefix: str) -> int:
            _, rows = fetch(path, body_of(prefix))
            ids.update(rows)
            n = len(rows)
            print(f"{prefix}:{n}{'*' if n >= TRUNCATION_SENTINEL else ' '}".rjust(9), end="", flush=True)
            return n

        dist = [(ch, bucket(ch)) for ch in SHARD_CHARS]
        print()
        sizes = sorted(n for _, n in dist)
        hot = [ch for ch, n in dist if n >= TRUNCATION_SENTINEL]
        hot2: list[str] = []
        for ch in hot:
            print(f"  two-char under {ch!r}: ", end="", flush=True)
            for ch2 in SHARD_CHARS:
                if bucket(ch + ch2) >= TRUNCATION_SENTINEL:
                    hot2.append(ch + ch2)
            print()
        print(f"  [INFO] {label}: union={len(ids)} distinct IDs; one-char sizes "
              f"min={sizes[0]} median={sizes[len(sizes)//2]} max={sizes[-1]} empty={sizes.count(0)}")
        if hot:
            print(f"  [INFO] {label}: one-char buckets still capped at 100: {hot[:10]} ({len(hot)} total)")
        if hot2:
            print(f"  [INFO] {label}: still capped after 2-char subdivision: {hot2[:10]} ({len(hot2)} total)")
        if sizes[-1] == 0:
            print(f"  [VERDICT] {label}: baseline rows={len(base_ids)} and EVERY one-char term -> 0. "
                  "Either the term is matched only at length >1 / exact, or this body is not the "
                  "endpoint's real schema (a 0-row baseline is indistinguishable from a wrong shape). "
                  "Prefix sharding is not viable here; use the ID sweep.")
        elif len(ids) > TRUNCATION_SENTINEL and not hot2:
            print(f"  [VERDICT] {label}: prefix sharding BREAKS the cap — union={len(ids)} with every "
                  "bucket under 100. Wire two-level prefix sharding into this search.")
        elif len(ids) > TRUNCATION_SENTINEL:
            print(f"  [VERDICT] {label}: union={len(ids)} but {len(hot2)} 2-char bucket(s) still hit "
                  "the cap -> add a second key (status, quick-search field) beneath the prefix.")
        else:
            print(f"  [VERDICT] {label}: union={len(ids)} <= baseline+eps -> terms match but the "
                  "bucketed field cannot distinguish them. Dead key.")

    shard_by_term(
        "/Contract/Search",
        lambda term: {"SearchTerm": term, "IncludeActive": True, "IncludeInactive": True},
        "/Contract/Search",
    )
    if live_qsf:
        shard_by_term(
            "/Client/Search",
            lambda term: {"SearchTerm": term, "QuickSearchField": live_qsf[0]},
            f"/Client/Search (QSF={live_qsf[0]})",
        )
    shard_by_term(
        "/Client/AdvancedSearch",
        lambda term: {"SearchTerm": term},
        "/Client/AdvancedSearch",
    )

    # Second key: contract status buckets. Cheap and exact if statuses partition
    # the contract table; overlaps are harmless (union is a set), gaps are fatal.
    ids_status: set[str] = set()
    per: dict[int, int] = {}
    for st in range(6):
        _, rows = fetch("/Contract/Lookup", {"SearchTerm": "", "IncludeStatuses": [st]})
        per[st] = len(rows)
        ids_status.update(rows)
    print(f"  [INFO] /Contract/Lookup per-status rows: {per}")
    print(f"  [INFO] /Contract/Lookup union over statuses 0-5: {len(ids_status)} distinct IDs")
    if max(per.values()) >= TRUNCATION_SENTINEL:
        print("  [INFO] a single status bucket is itself capped -> status alone is not enough; "
              "combine status x prefix.")
    elif len(ids_status) > TRUNCATION_SENTINEL:
        print(f"  [VERDICT] /Contract/Lookup status sharding recovers {len(ids_status)} contracts "
              "vs the 100-row cap -> usable as the contract enumeration spine.")

    # Does the union of BOTH keys beat either alone? (coverage sanity, not proof)
    _, capped = fetch("/Contract/Search", {"SearchTerm": "", "IncludeActive": True, "IncludeInactive": True})
    both = ids_status | set(capped)
    print(f"  [INFO] status-union ∪ contract-search baseline = {len(both)} distinct IDs")

    try:
        client.delete("/Session/Delete")
    except Exception:
        pass
    client.close()


# --------------------------------------------------------------------------
# ID SWEEP: the cap-immune enumeration spine
# --------------------------------------------------------------------------
def probe_id_sweep() -> None:
    """SearchByID inverts the cap problem: the server caps what it RETURNS, but the
    CALLER controls the requested ID list, so a batch of <=50 IDs can never reach
    the 100-row cap and found<=asked is verifiable per call. The probe finds the
    high-water ID by doubling, sweeps 1..max in batches of 50 until four consecutive
    batches come back empty (200 dead IDs at the tail), and checks the sweep CONTAINS
    the capped /Get baseline — if any baseline ID is missing, SearchByID answers a
    different key space (internal ID vs the UI's ClientCD) and the spine is void."""
    s = _settings()
    base = (s.aquira_base_url or "").rstrip("/")
    if not (base and s.aquira_username and s.aquira_password):
        print("IDS: aquira credentials required")
        return
    client = httpx.Client(base_url=base, timeout=SHARD_TIMEOUT)
    login = client.post(
        "/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password}
    ).json()
    if not login.get("Success", True):
        print("  [FAIL] login refused")
        client.close()
        return
    print("\n=== ID SWEEP via SearchByID (reads only) ===")
    BATCH = 50
    DEAD_TAIL_RUNS = 4
    MAX_BATCHES = 400  # 20k IDs ceiling; a bigger tenant must narrow by other means

    def by_id(path: str, ids: list[int]) -> list[str]:
        try:
            r = client.request("POST", path, json={"SearchIDs": ids})
            try:
                return _row_ids(r.json())
            except Exception:
                return []
        except Exception:
            return []

    for endpoint, get_path in (("/Client", "/Client/Get"), ("/Contract", "/Contract/Get")):
        print(f"\n  --- {endpoint} ---")
        try:
            baseline = _row_ids(client.request("GET", get_path).json())
        except Exception:
            baseline = []
        path = f"{endpoint}/SearchByID"
        powers = [2 ** k for k in range(0, 17)]
        alive = [a for a in by_id(path, powers) if str(a).isdigit()]
        if not alive:
            print(f"  [FAIL] {path} answers nothing for IDs {powers[:6]}... — no ID spine here. "
                  "SearchByID is unusable; fall back to status/field bucketing.")
            continue
        top = max(int(a) for a in alive)
        print(f"  [INFO] doubling probe: powers of two up to {top} exist; "
              f"capped {get_path} baseline rows={len(baseline)}")

        found: set[str] = set()
        empty_run = batches = 0
        ceiling_hit = True
        start = 1
        print("  sweep rows/batch: ", end="", flush=True)
        while batches < MAX_BATCHES:
            ids = list(range(start, start + BATCH))
            rows = by_id(path, ids)
            found.update(rows)
            batches += 1
            print(f"{len(rows)}  ", end="", flush=True)
            if batches % 15 == 0:
                print("\n               ", end="", flush=True)
            if rows:
                empty_run = 0
            else:
                empty_run += 1
                if empty_run >= DEAD_TAIL_RUNS:
                    ceiling_hit = False
                    break
            start += BATCH
        print()
        missing = [i for i in baseline if i not in found]
        tail = "CEILING hit (tenant larger than 20k IDs) — sweep is PARTIAL" if ceiling_hit \
            else f"stopped after {DEAD_TAIL_RUNS * BATCH} consecutive dead IDs at {start - BATCH}"
        print(f"  [INFO] {endpoint}: examined IDs 1..{start - 1} in {batches} batches of <=50 -> "
              f"{len(found)} existing rows. {tail}")
        if missing:
            print(f"  [FAIL] {len(missing)} of the {len(baseline)} capped-baseline IDs are NOT in the "
                  f"sweep ({missing[:6]}...). SearchByID answers a different key space than /Get rows — "
                  "do NOT wire this spine until that is explained.")
        elif len(found) > len(baseline):
            print(f"  [VERDICT] {endpoint}: sweep recovers {len(found)} rows, strictly contains the "
                  f"{len(baseline)}-row capped view, and no batch neared the cap -> COMPLETE enumeration. "
                  "Make the SearchByID sweep the enumeration spine in app/aquira/client.py.")
        else:
            print(f"  [INFO] {endpoint}: sweep found {len(found)} == baseline -> the table fits under "
                  "the cap TODAY; the sweep is still the right spine because it cannot silently lose rows.")
        if top:
            print(f"  [INFO] {endpoint}: ID density = {len(found)}/{top} live below high-water {top} "
                  f"({100.0 * len(found) / top:.0f}%)")

    try:
        client.delete("/Session/Delete")
    except Exception:
        pass
    client.close()


# --------------------------------------------------------------------------
# SWEEP DEAD TAIL: can SearchByID prove the end of the ID space, and does the
# sweep pull real data (not just ids)?
# --------------------------------------------------------------------------

# Fields that must survive normalize() for a sweep row to be usable at all.
# Deliberately DERIVED names, not raw row keys: SearchByID rows are the same POCO
# the capped /Get view returns (proven 2026-09-24), so Email/Contacts live in
# /Client/Load, not here — reporting their raw absence as a FAIL was a false alarm.
SWEEP_DERIVED_REQUIRED = {
    "Client": ("ID", "Name"),
    "Contract": ("ID", "ContractCD", "Status"),
}

# Raw SearchByID row keys that are NOT an integrity problem by themselves, because
# load_catalog merges the per-record Load payload over the sweep row.
SUPPLIED_BY_LOAD = ("Email", "Contacts", "Version", "Attributes")

# Keys that could carry an owner, so a rep/team field that shows up only on the Load
# payload is visible as such instead of being mistaken for a normalize bug.
_OWNERISH_RE = re.compile(r"(?i)rep|team|sales|user|booked|owner|\bae\b|agent")

# The envelope gotcha that made the first run of this section lie to itself: Aquira
# answers SUCCESSFUL calls with ErrorName as the STRING "None" (the enum's name for
# "no error"), while real failures carry ErrorName "NotFound" (Error -12) or
# "InvalidArgument" (Error -27). Anything that treats a present ErrorName as a
# failure marks every good batch dead, stops the ID map after 3 batches, and prints a
# verdict contradicting its own http/Success columns. Ignore these.
NO_ERROR_TOKENS = ("", "none", "null", "0")


def probe_sweep_tail() -> None:
    """Two questions this answers, both load-bearing for the enumeration spine.

    (1) THE DEAD TAIL. app/aquira/client.py:sweep_enumerate certifies a complete
    enumeration only after SWEEP_DEAD_TAIL_RUNS consecutive batches return ZERO rows.
    That proof exists only if a zero-match batch comes back HTTP 200 with an empty
    list. It does not go through httpx directly: every sweep call is try_request ->
    request, and request RAISES when status >= 400 **or** payload["Success"] is False,
    so try_request returns None. In sweep_enumerate `payload is None` breaks the loop
    and appends truncated_sources — "sweep stopped unproven". The production log shows
    BOTH resources going partial at exactly the first fully-dead ID range, which is
    that signature. This section prints the server's verbatim answer for zero-match
    batches (http status, Success, ErrorName/ErrorText) so the fix can match on it, and
    tests the fix that answer implies: seed every batch with one id known to exist (a
    sentinel) so zero-match is unreachable and an all-dead range reads as
    "only the sentinel came back". The mixed 49-dead + 1-live call below is the
    precondition for that; the sweep must also stop counting the sentinel as a hit.

    (2) THE DATA PULL. A sweep row that only carries an ID is an enumeration, not a
    pull. This reports the key union across every live SearchByID row, diffs it against
    the capped /{Resource}/Get view, runs the real normalize_* over every sweep row, and
    shows the booked/proposal/inactive split the run would have written — so "complete
    enumeration" can be checked for content too.

    Bonus: whether SearchByID is row-capped at all, and how many ids one call will take
    (batch sizing drives run duration). The first run answered this: 400 requested ids
    returned 279 rows, so it is NOT capped — which is why the app's integrity check is
    now "every returned id was requested", not "rows < 100". That invariant is checked
    here over every live batch.

    READ-ONLY: SearchByID, /{Client,Contract}/Get, /Client/Search, /Contract/Lookup,
    plus login/logout. No Edit/Put/Create/Delete.
    """
    s = _settings()
    base = (s.aquira_base_url or "").rstrip("/")
    if not (base and s.aquira_username and s.aquira_password):
        print("TAIL: aquira credentials required")
        return
    client = httpx.Client(base_url=base, timeout=SHARD_TIMEOUT)
    try:
        login = client.post(
            "/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password}
        ).json()
    except Exception:
        login = {}
    if not login.get("Success", True):
        print("  [FAIL] login refused")
        client.close()
        return
    print("\n=== SWEEP DEAD TAIL + DATA PULL (reads only) ===")

    BATCH = 50
    SCAN_CEIL = 2500  # id ceiling for the live map; tenants above it need a wider probe

    def call(resource: str, ids: list[int]) -> dict[str, Any]:
        """One SearchByID call, described in full — the error text IS the answer."""
        path = f"/{resource}/SearchByID"
        out: dict[str, Any] = {"http": None, "Success": None, "rows": None, "row_dicts": []}
        try:
            r = client.request("POST", path, json={"SearchIDs": ids})
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
            return out
        out["http"] = r.status_code
        try:
            payload = r.json()
        except Exception:
            out["error"] = "non-JSON " + (r.text or "")[:150].replace("\n", " ")
            return out
        if not isinstance(payload, dict):
            out["error"] = f"json {type(payload).__name__}"
            return out
        out["Success"] = payload.get("Success")
        rows = payload.get("Data")
        if not isinstance(rows, list):
            rows = payload.get("Items") if isinstance(payload.get("Items"), list) else []
        out["rows"] = len(rows)
        out["row_dicts"] = [row for row in rows if isinstance(row, dict)]
        for key in ("ErrorName", "ErrorText", "Error"):
            value = payload.get(key)
            if value in (None, 0, "0", False):
                continue
            if isinstance(value, str) and value.strip().lower() in NO_ERROR_TOKENS:
                continue  # "None" is this API's spelling of no-error
            out[key] = str(value)[:200]
        return out

    def bad(res: dict[str, Any]) -> bool:
        """Exactly what the app sees: request() raises on status >= 400 **or**
        Success is False, try_request turns the raise into None, and None breaks the
        sweep loop. A present-but-benign ErrorName ("None") is NOT a failure — the
        first run of this section got that wrong and every line after it was noise."""
        http = res.get("http")
        return (
            http is None
            or (isinstance(http, int) and http >= 400)
            or res.get("Success") is False
            or res.get("error") is not None
        )

    def show(label: str, res: dict[str, Any]) -> None:
        print(
            f"    {label:<34} http={res.get('http')} Success={res.get('Success')} rows={res.get('rows')}"
            + ("  -> app reads this as FAILED (sweep breaks)" if bad(res) else "")
        )
        for key in ("ErrorName", "ErrorText", "Error", "error"):
            if key in res:
                print(f"    {'':<34} {key}={res[key]!r}")

    def ident_of(row: dict[str, Any]) -> int | None:
        raw = row.get("ID") or row.get("Id") or row.get("id")
        return int(raw) if str(raw).isdigit() else None

    def shape(value: Any) -> str:
        """Type + skeleton of a value, short enough to read one per line. What a field
        IS (list of records? a display string? a FieldValue wrapper?) is the only way to
        tell why normalize finds nothing in a key that is plainly present."""
        if isinstance(value, list):
            return f"list[{len(value)}]{{{'empty' if not value else shape(value[0])}}}"
        if isinstance(value, dict):
            return "dict{" + ",".join(sorted(str(k) for k in value)[:8]) + "}"
        if isinstance(value, str):
            return f"str:{value[:60]!r}"
        return f"{type(value).__name__}:{value!r}"

    # load_catalog also pulls the rep roster, and app/mapping/owners.py:expand_owner_lookup
    # indexes each rep by BOTH its User/Lookup ID and its SalesRepID (plus name). So a
    # record's SalesRepID only resolves to a HubSpot owner if it is in that combined key
    # set — the two id spaces are not the same and printing only one of them would make
    # a valid rep id look unknown.
    rep_ids: set[str] = set()
    rep_sales_ids: set[str] = set()
    rep_names: set[str] = set()
    try:
        r = client.request("POST", "/User/Lookup", json={
            "salesReps": True, "CurrentOnly": True, "SearchTerm": "",
        })
        p = r.json() if r.content else {}
        rep_rows = [row for row in ((p or {}).get("Data") or []) if isinstance(row, dict)]
        for row in rep_rows:
            for key in ("ID", "Id"):
                if row.get(key) not in (None, ""):
                    rep_ids.add(str(row[key]))
            if row.get("SalesRepID") not in (None, ""):
                rep_sales_ids.add(str(row["SalesRepID"]))
            for key in ("Name", "LongName"):
                if row.get(key):
                    rep_names.add(str(row[key]).strip().lower())
        print(f"\n  [INFO] /User/Lookup(salesReps) http={r.status_code} rows={len(rep_rows)}")
        print(f"  [INFO] roster User/Lookup IDs  = {sorted(rep_ids, key=lambda s: int(s) if s.isdigit() else 0)}")
        print(f"  [INFO] roster SalesRepIDs      = {sorted(rep_sales_ids, key=lambda s: int(s) if s.isdigit() else 0)}")
        print(f"  [INFO] roster names            = {sorted(rep_names)}")
        if rep_rows:
            print(f"  [INFO] rep row keys: {sorted(rep_rows[0])}")
    except Exception as exc:
        print(f"\n  [INFO] /User/Lookup(salesReps) -> {type(exc).__name__}")
    rep_all_ids = rep_ids | rep_sales_ids

    for resource in ("Client", "Contract"):
        print(f"\n  --- /{resource}/SearchByID ---")

        # (1) map the live id space, batch by batch, the way the sweep does. A
        # zero-match batch is an ERROR for this API (see the shapes below), so an
        # empty batch and a failed batch both legitimately mean "150 dead ids".
        live: dict[int, dict[str, Any]] = {}
        profile: list[str] = []
        stray_ids: list[Any] = []
        empties = 0
        scanned = 0
        start = 1
        while start <= SCAN_CEIL:
            ids = list(range(start, start + BATCH))
            res = call(resource, ids)
            n = res.get("rows") or 0
            scanned += 1
            returned = [ident_of(row) for row in res.get("row_dicts", [])]
            stray_ids.extend(a for a in returned if a not in set(ids))
            profile.append(f"{start}-{start + BATCH - 1}:{n}{'!' if bad(res) else ''}")
            for row in res.get("row_dicts", []):
                ident = ident_of(row)
                if ident is not None:
                    live[ident] = row
            empties = 0 if (n and not bad(res)) else empties + 1
            if live and empties >= 3:
                break
            start += BATCH
        if not live:
            print(f"  [FAIL] no live {resource} ids found in {scanned} batches up to "
                  f"{start + BATCH - 1} — cannot probe the tail. SearchByID is unusable here; "
                  "the enumeration spine must come from elsewhere.")
            continue
        low, high = min(live), max(live)
        stopped = ("after 3 consecutive dead batches — the real tail" if empties >= 3
                   else f"at the {SCAN_CEIL} id scan ceiling — tail UNKNOWN")
        print(f"  [INFO] {resource}: {len(live)} live ids, range {low}..{high}; map stopped {stopped} "
              f"(id {start + BATCH - 1}, {scanned} batches)")
        print(f"  [INFO] rows per batch of {BATCH} ('!' = the app's try_request returns None, sweep breaks):")
        for i in range(0, len(profile), 6):
            print(f"           {'  '.join(profile[i:i + 6])}")

        # The invariant sweep_enumerate now certifies on: the server must answer ONLY
        # ids it was handed. One stray means the ID list is being ignored, which is
        # uncountable by construction — and row count alone can never show it, because
        # SearchByID is not row-capped.
        _show(f"{resource}: SearchByID honoured the id list on every batch", not stray_ids,
              f"{len(stray_ids)} stray row id(s): {stray_ids[:8]}" if stray_ids
              else f"{scanned} batches, every returned id was requested")

        # (2) THE question: a batch in which no id exists, at the exact place the
        # sweep would meet it — the first all-dead range above high-water.
        first_dead = ((high - 1) // BATCH + 1) * BATCH + 1
        print("  dead-tail shapes (what sweep_enumerate needs to survive):")
        tail = call(resource, list(range(first_dead, first_dead + BATCH)))
        show(f"zero-match {first_dead}..{first_dead + BATCH - 1}", tail)
        show("zero-match 5 far ids", call(resource, [90001, 90002, 90003, 90004, 90005]))
        show("zero-match single id", call(resource, [97000]))
        show("zero-match out-of-range 1e9", call(resource, [10 ** 9]))
        show("empty SearchIDs []", call(resource, []))
        sentinel = high
        mixed_ids = [sentinel] + [i for i in range(90001, 90051) if i != sentinel][:49]
        mixed = call(resource, mixed_ids)
        show(f"sentinel {sentinel} + 49 dead", mixed)
        mixed_hits = [ident_of(row) for row in mixed.get("row_dicts", [])]
        print(f"           mixed batch returned ids {mixed_hits} (want exactly [{sentinel}]) — "
              "this is the fix's precondition")
        # And the same batch with the sentinel deleted, which is how the sweep fails
        # closed: if the sentinel stops answering, nothing distinguishes "deleted"
        # from "endpoint broken", so PARTIAL is the only honest verdict.
        show("sentinel-only batch (high id)", call(resource, [sentinel]))

        if not bad(tail) and tail.get("rows") == 0:
            print(f"  [VERDICT] {resource}: a zero-match batch IS HTTP 200 + empty Data, so the "
                  "dead-tail proof is satisfiable as written. The production PARTIAL came from "
                  "something else — the '!' batches and the shapes above name it.")
        elif bad(tail):
            print(f"  [VERDICT] {resource}: a zero-match batch is answered as an ERROR "
                  f"(http={tail.get('http')} Success={tail.get('Success')} "
                  f"ErrorName={tail.get('ErrorName')!r} Error={tail.get('Error')!r}), so "
                  "try_request returns None, the sweep loop breaks and appends "
                  "truncated_sources. The tail can NEVER be proven: every full run is "
                  "PARTIAL by construction, for both resources, at the first fully-dead "
                  "id range — exactly the production warning.")
        else:
            print(f"  [UNEXPECTED] {resource}: zero-match batch is not an error but returned "
                  f"{tail.get('rows')} rows — the id map is wrong; report verbatim.")
        if bad(tail) and not bad(mixed) and mixed_hits == [sentinel]:
            print(f"  [NEXT] {resource}: sentinel batches work, so the fix is what sweep_enumerate "
                  "now does — seed every batch with one id known to exist (the doubling probe's "
                  "high-water) and do not count that row as a hit. Zero-match becomes "
                  "unreachable and the 4-empty-run proof becomes real. The alternative "
                  "(teach try_request to read this exact ErrorName as 'no rows') is weaker: it "
                  "trusts one tenant's error vocabulary and would mask a genuine outage.")

        # (3) is the sweep a DATA pull or just an id list? Judge the NORMALIZED
        # output, not the raw row keys: load_catalog merges /{Resource}/Load over
        # every sweep row, so a row without Email or Contacts is by design. What
        # would be a real defect is a row that will not normalize at all.
        seen: set[str] = set()
        for row in live.values():
            seen.update(row)
        print(f"  [INFO] {resource}: union of sweep-row keys = {len(seen)}: {sorted(seen)}")
        absent_raw = [k for k in SWEEP_DERIVED_REQUIRED[resource] if k not in seen]
        if absent_raw:
            print(f"  [INFO] {resource}: raw keys {absent_raw} are not on the sweep row — normalize "
                  "reaches them through aliases or the per-record Load; the derived check below is "
                  "the one that matters.")

        from app.aquira.normalize import normalize_client, normalize_contract

        fn = normalize_client if resource == "Client" else normalize_contract
        norm = [fn(row) for row in live.values()]
        ok = [n for n in norm if n]
        _show(f"{resource}: normalize() accepts sweep rows", len(ok) == len(live),
              f"{len(ok)}/{len(live)} rows" + ("" if len(ok) == len(live) else
              f" — {len(live) - len(ok)} row(s) would be invisible to the run"))
        holes = [
            f"{key}={sum(1 for c in ok if c.get(key))}" for key in SWEEP_DERIVED_REQUIRED[resource]
        ]
        blank = [h for h in holes if h.endswith("=0")]
        _show(f"{resource}: sweep rows carry usable data", not blank,
              " ".join(holes) + (f"  <- {blank} empty on EVERY row" if blank else ""))
        try:
            get_rows = client.request("GET", f"/{resource}/Get").json()
            get_keys: set[str] = set()
            for row in (get_rows.get("Data") if isinstance(get_rows, dict) else None) or []:
                if isinstance(row, dict):
                    get_keys.update(row)
            only_get = sorted(get_keys - seen)
            print(f"  [INFO] {resource}: fields the capped /{resource}/Get view has but the "
                  f"sweep rows do NOT ({len(only_get)}): {only_get}")
            print(f"  [INFO] {resource}: sweep-only fields (SearchByID enrichment): "
                  f"{sorted(seen - get_keys)}")
        except Exception as exc:
            print(f"  [INFO] {resource}: /{resource}/Get comparison skipped ({type(exc).__name__})")

        load_needing = [k for k in SUPPLIED_BY_LOAD if k not in seen]
        if resource == "Client" and ok:
            print(f"  [INFO] Client pull: named={sum(1 for c in ok if c.get('Name'))} "
                  f"email={sum(1 for c in ok if c.get('Email'))} "
                  f"phone={sum(1 for c in ok if c.get('Phone'))} "
                  f"contacts={sum(len(c.get('Contacts') or []) for c in ok)} "
                  f"account={sum(1 for c in ok if c.get('IsAccount'))} "
                  f"advertiser={sum(1 for c in ok if c.get('IsAdvertiser'))} "
                  f"rep-id={sum(1 for c in ok if c.get('SalesRepID'))} "
                  f"rep-name={sum(1 for c in ok if c.get('SalesRepName'))} "
                  f"teams={sum(1 for c in ok if c.get('SalesTeams'))} "
                  f"-> {len(load_needing)} field(s) only /Client/Load can supply {load_needing}")
        if resource == "Contract" and ok:
            print(f"  [INFO] Contract pull: booked={sum(1 for c in ok if c.get('IsContract'))} "
                  f"proposal={sum(1 for c in ok if c.get('IsProposal') and not c.get('IsContract'))} "
                  f"cancelled={sum(1 for c in ok if c.get('Cancelled'))} "
                  f"inactive={sum(1 for c in ok if c.get('IsActive') is False)} "
                  f"active-unknown={sum(1 for c in ok if c.get('IsActive') is None)} "
                  f"with-total={sum(1 for c in ok if c.get('TotalValue'))} "
                  f"with-dates={sum(1 for c in ok if c.get('StartDate') and c.get('EndDate'))} "
                  f"with-lines={sum(1 for c in ok if c.get('lines'))} "
                  f"rep={sum(1 for c in ok if c.get('SalesRepID') or c.get('SalesRepName'))} "
                  f"teams={sum(1 for c in ok if c.get('SalesTeams'))}")
            if not any(c.get("lines") for c in ok):
                print(f"  [INFO] {resource}: sweep rows carry NO line data (keys include "
                      f"{sorted(k for k in seen if 'Line' in k or 'Spot' in k)}) — revenue periods "
                      "must still come from GetContractDetailAnalysis/GetSpotLineDetailAnalysis per "
                      "contract, so the sweep cannot replace the per-record detail reads.")

        # (4) is SearchByID row-capped, and how large may a batch be? Run duration and
        # the app's integrity check both hinge on this. An exact 100 rows is NOT the
        # tell (100 requested ids can all be live); the tell is rows vs the live ids
        # already known to sit inside the requested range.
        print(f"  [INFO] {resource}: batch sizing (rows vs live ids known in range):")
        widest = 0
        truncated_at = 0
        for size in (50, 100, 200, 400, 800):
            expect = sum(1 for a in live if a <= size)
            res = call(resource, list(range(1, size + 1)))
            n = res.get("rows")
            note = ""
            if isinstance(n, int):
                if n < expect:
                    note = f"  <- TRUNCATED: {expect - n} of {expect} live ids in range unanswered"
                    truncated_at = max(truncated_at, n)
                else:
                    widest = max(widest, n)
            print(f"           {size:>4} ids -> http={res.get('http')} Success={res.get('Success')} "
                  f"rows={n} live-in-range={expect}{note}")
        if widest > TRUNCATION_SENTINEL:
            print(f"  [VERDICT] {resource}: SearchByID is NOT row-capped — one call answered "
                  f"{widest} rows. So a rows>=100 tripwire on THIS endpoint is a false positive, and "
                  "the only sound integrity check is returned-ids ⊆ requested-ids (checked above, and "
                  "now what sweep_enumerate certifies). SWEEP_BATCH is a duration knob, not a "
                  "correctness knob.")
        elif truncated_at:
            print(f"  [VERDICT] {resource}: SearchByID DID truncate at {truncated_at} rows — keep "
                  f"SWEEP_BATCH <= 50 and keep the row-count tripwire.")
        else:
            print(f"  [INFO] {resource}: no batch answered more than {TRUNCATION_SENTINEL} rows, so "
                  "this run cannot settle the cap. Keep SWEEP_BATCH <= 50 (safe either way).")

        # (5) is an empty result set this API's general behavior, or SearchByID-specific?
        try:
            r = client.request("POST", "/Client/Search", json={
                "SearchTerm": "zzqx no such client zzqx", "QuickSearchField": 8,
                "IncludeActive": True, "IncludeInactive": True,
            })
            p = r.json() if r.content else {}
            p = p if isinstance(p, dict) else {}
            print(f"  [INFO] control /Client/Search no-match -> http={r.status_code} "
                  f"Success={p.get('Success')} rows={_count(p, 'Data')} "
                  f"ErrorName={str(p.get('ErrorName'))[:80]!r}")
        except Exception as exc:
            print(f"  [INFO] control /Client/Search no-match -> {type(exc).__name__}")
        try:
            r = client.request("POST", "/Contract/Lookup", json={"SearchTerm": "", "IncludeStatuses": [99]})
            p = r.json() if r.content else {}
            p = p if isinstance(p, dict) else {}
            print(f"  [INFO] control /Contract/Lookup status 99 -> http={r.status_code} "
                  f"Success={p.get('Success')} rows={_count(p, 'Data')} "
                  f"ErrorName={str(p.get('ErrorName'))[:80]!r}")
        except Exception as exc:
            print(f"  [INFO] control /Contract/Lookup status 99 -> {type(exc).__name__}")

        # (6) The counts above say rep-id=0 and rep-name=0 on EVERY client row even
        # though SalesReps and SalesTeams are keys on that row, and rep=0/teams=0 on
        # every contract row. Either the value is a shape normalize does not read
        # (fixable with an alias) or Aquira genuinely does not fill it (not our bug).
        # So dump the SHAPES, then re-ask the question the way load_catalog actually
        # assembles a record: sweep row -> normalize -> /{Resource}/Load -> normalize
        # -> merge_*, because a field blank on the summary can arrive with the Load.
        print(f"  [INFO] {resource}: owner/team-ish fields on sweep rows (shape, not just presence):")
        for key in ("SalesReps", "SalesTeams", "SalesTeam", "BookedBy", "CopyWriter",
                    "Type", "Account", "Advertiser", "Status"):
            present = [row for row in live.values() if key in row]
            if not present:
                continue
            blank = sum(1 for row in present if row[key] in (None, "", [], {}, 0, False))
            print(f"           {key:<11} on {len(present):>4}/{len(live)} rows, blank on {blank:>4}, "
                  f"e.g. {shape(present[0][key])}")

        ordered = sorted(live)
        sample_ids = sorted({low, high, ordered[len(ordered) // 3], ordered[2 * len(ordered) // 3]})
        print(f"  [INFO] {resource}: assembled record for {len(sample_ids)} sampled ids "
              f"({sample_ids}) — sweep row -> /{resource}/Load -> merge:")
        from app.aquira.normalize import entity_of, merge_client, merge_contract

        merge = merge_client if resource == "Client" else merge_contract
        got_rep = got_teams = resolvable = sampled = 0
        for ident in sample_ids:
            try:
                if resource == "Client":
                    lp = client.request("POST", f"/Client/Load/{ident}").json()
                else:
                    lp = client.request("POST", f"/Contract/Load/{ident}", json={"name": "probe"}).json()
            except Exception as exc:
                print(f"           id {ident}: /{resource}/Load raised {type(exc).__name__}")
                continue
            if not isinstance(lp, dict):
                print(f"           id {ident}: /{resource}/Load returned {type(lp).__name__}")
                continue
            summary = fn(live[ident]) or {}
            merged = merge(summary, fn(lp)) or summary
            rep = merged.get("SalesRepID") or merged.get("SalesRepName")
            rep_id = merged.get("SalesRepID")
            teams = merged.get("SalesTeams") or []
            sampled += 1
            got_rep += 1 if rep else 0
            got_teams += 1 if teams else 0
            if rep_id is not None and str(rep_id) in rep_all_ids:
                resolvable += 1
            load_keys = set(entity_of(lp)) - set(live[ident])
            repish = sorted(k for k in load_keys if _OWNERISH_RE.search(str(k)))
            note = f"  Load-only owner-ish keys={repish}" if repish else ""
            if rep_id is not None:
                note += (f"  SalesRepID={rep_id!r} "
                         f"in-roster={str(rep_id) in rep_all_ids}")
            if resource == "Contract":
                booked_by = live[ident].get("BookedBy")
                if booked_by not in (None, "", 0):
                    # BookedBy is a display NAME on this tenant's rows, not a user id.
                    # Compare like for like or the check reads False whoever it names;
                    # plan_deals never looks at this field, so it is context only.
                    note += (f"  BookedBy={shape(booked_by)} "
                             f"name-in-roster={str(booked_by).strip().lower() in rep_names}")
            print(f"           id {ident}: MERGED rep={rep!r} teams={len(teams)}{note}")
        _show(f"{resource}: assembled record carries a sales rep", got_rep > 0,
              f"{got_rep}/{sampled} sampled ids")
        _show(f"{resource}: assembled record carries team names", got_teams > 0,
              f"{got_teams}/{sampled} sampled ids")
        _show(f"{resource}: that rep id resolves to a HubSpot owner", resolvable > 0,
              f"{resolvable}/{sampled} sampled ids hit the /User/Lookup roster (User.ID ∪ "
              f"SalesRepID) — the rest need an owner-map row (app/mapping/owners.py:"
              f"expand_owner_lookup) or the team->owner map, or the deal lands unowned")
        if sampled and rep_all_ids and resolvable < sampled:
            print(f"  [NEXT] {resource}: the roster call is filtered salesReps+CurrentOnly, so a "
                  "manager, a copywriter, or an off-roster rep id will not resolve from /User/Lookup "
                  "alone. Those records need an admin owner-map row or a team mapping; check the "
                  "owners page covers the ids printed above.")
        if sampled and not got_rep and not got_teams:
            print(f"  [NEXT] {resource}: neither the sweep row nor /{resource}/Load yields an owner "
                  "OR a team, so HubSpot deals land unowned and the owner-map cannot match by team. "
                  "The shapes above decide whether normalize.py needs an alias or this tenant does "
                  "not fill the field.")
        elif sampled and not got_rep and got_teams:
            print(f"  [NEXT] {resource}: teams resolve, rep does not. Deal ownership is keyed on the "
                  "CONTRACT's SalesRepID (app/sync/planner.py:433-435 -> owner_by_aquira_user), "
                  "falling back to a hubspot_owner_id the team map may have set "
                  "(app/mapping/teams.py:379). So rep=0 on the assembled record means deals are "
                  "owned only through team->owner, and only for teams the admin mapped. The shapes "
                  "above say whether normalize.py needs an alias (BookedBy/UserID/SalesReps shape) "
                  "or this tenant leaves the rep genuinely blank.")

    print("  [NEXT] Paste this whole section back. The dead-tail VERDICT/NEXT lines and the "
          "owner/team block at the end of each resource are the parts the code depends on.")
    try:
        client.delete("/Session/Delete")
    except Exception:
        pass
    client.close()


# --------------------------------------------------------------------------
# READ-PATH CENSUS: how many failed_reads a full run is obliged to record, and why
# --------------------------------------------------------------------------
def probe_read_census() -> None:
    """Why this section exists. A run reported:

        Revenue pruning suppressed: the Aquira pull is not certified complete
        (84 failed read(s)); 248 contract(s) were visible to this run.

    with NO "source(s) hit the Aquira row cap" cause and NO "contract detail load(s)
    failed" cause, which means the SearchByID sweeps DID prove their tail and every
    /Contract/Load answered. The only remaining gate is
    app/aquira/client.py:`certified = ... and not self.failed_calls`, and try_request
    records a failed call for EVERY speculative endpoint attempt — the deliberate
    multi-guess fallbacks in load_spot_lines/load_charge_lines, and, critically, a
    "no rows" answer, which this API delivers as Success:false + ErrorName:"NotFound"
    rather than an empty list (proven for SearchByID in the tail section).

    If that convention holds for the revenue-detail endpoints too, then any tenant
    with line-less contracts can NEVER certify: pruning stays suppressed forever, no
    record is lost, and nothing in the log looks like an error. The failed_reads count
    is then a measurement of the tenant's empty contracts, not of pull quality — which
    is exactly the wrong thing to gate a destructive prune on.

    So measure it rather than assume it: call the endpoints a full run calls, with the
    bodies it sends, in the order load_contract tries them, over a stratified sample
    (contracts whose sweep row carried an amount vs. those that did not — the tail
    section counted 164 with and 84 without, and 84 is also the reported failed_reads,
    which is the coincidence worth killing). Then replay the app's own fallback logic
    to PREDICT failed_reads for the whole tenant.

    READ-ONLY: SearchByID, /Contract/Load/<id>, /Contract/GetContractDetailAnalysis,
    /Contract/GetSpotLineDetailAnalysis, /Contract/LoadSpotline, /User/Lookup.
    """
    s = _settings()
    base = (s.aquira_base_url or "").rstrip("/")
    if not (base and s.aquira_username and s.aquira_password):
        print("READS: aquira credentials required")
        return
    client = httpx.Client(base_url=base, timeout=TIMEOUT)
    try:
        login = client.post(
            "/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password}
        ).json()
    except Exception:
        login = {}
    if not login.get("Success", True):
        print("  [FAIL] login refused")
        client.close()
        return
    print("\n=== READ-PATH CENSUS (reads only) ===")

    from app.aquira.normalize import (
        normalize_charge_lines,
        normalize_contract,
        normalize_revenue_months,
        normalize_spot_lines,
    )

    BATCH = 50
    # Every failed attempt is classified, because the fix depends entirely on WHICH
    # kind of failure the 84 are: "no rows, delivered as an error" (benign — gating a
    # destructive prune on it means pruning can never be earned) versus a real 5xx or
    # transport failure (those SHOULD uncertify the pull).
    shapes = {"no-rows": 0, "http-4xx": 0, "http-5xx": 0, "transport": 0}

    def call(path: str, body: dict | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"http": None, "rows": 0, "payload": None, "failed": False}
        try:
            r = client.request("POST", path, json=body) if body is not None else client.request("POST", path)
        except Exception as exc:
            out.update(error=f"{type(exc).__name__}: {str(exc)[:120]}", failed=True)
            shapes["transport"] += 1
            return out
        out["http"] = r.status_code
        try:
            p = r.json()
        except Exception:
            out.update(error="non-JSON", failed=True)
            shapes["http-5xx" if r.status_code >= 500 else "http-4xx"] += 1
            return out
        if not isinstance(p, dict):
            out.update(error=f"json {type(p).__name__}", failed=True)
            shapes["transport"] += 1
            return out
        out["payload"] = p
        out["Success"] = p.get("Success")
        data = p.get("Data")
        out["rows"] = len(data) if isinstance(data, list) else 0
        # app/aquira/client.py:request raises on exactly this condition, so this is the
        # rule that decides whether the app would have recorded a failed_call. ErrorName
        # is reported but NOT folded in: on successful calls it is the string "None".
        failed = r.status_code >= 400 or p.get("Success") is False
        out["failed"] = failed
        if failed:
            if r.status_code >= 500:
                shape = "http-5xx"
            elif r.status_code >= 400:
                shape = "http-4xx"
            else:
                shape = "no-rows"  # HTTP 200 + Success:false == this API's "empty"
            out["shape"] = shape
            shapes[shape] += 1
            for key in ("ErrorName", "ErrorText", "Error"):
                value = p.get(key)
                if value in (None, "", 0, "0", False):
                    continue
                if isinstance(value, str) and value.strip().lower() in NO_ERROR_TOKENS:
                    continue
                out[key] = str(value)[:120]
        return out

    # ---- map the contract id space and stratify by whether the sweep row has money --
    live: dict[int, dict[str, Any]] = {}
    empties = 0
    start = 1
    while start <= 1200:
        res = call("/Contract/SearchByID", {"SearchIDs": list(range(start, start + BATCH))})
        for row in (res.get("payload") or {}).get("Data") or []:
            if isinstance(row, dict):
                ident = row.get("ID") or row.get("Id")
                if str(ident).isdigit():
                    live[int(ident)] = row
        empties = 0 if (res.get("rows") and not res.get("failed")) else empties + 1
        if live and empties >= 3:
            break
        start += BATCH
    if not live:
        print("  [FAIL] no contracts found — nothing to census.")
        client.close()
        return
    rich = [i for i in sorted(live) if (normalize_contract(dict(live[i])) or {}).get("TotalValue")]
    rich_set = set(rich)
    poor = [i for i in sorted(live) if i not in rich_set]
    print(f"  [INFO] contracts enumerated: {len(live)} (high id {max(live)})")
    print(f"  [INFO] strata: sweep row HAS an amount = {len(rich)}, NO amount = {len(poor)}")
    print("  [INFO] the tail section counted 164 with-total / 84 without, and the run reported "
          "84 failed_reads. If those 84 empty-amount contracts are the same records that produce "
          "the failed reads, the number is tenant shape, not pull damage.")

    sample = poor[:10] + rich[:6]
    print(f"  [INFO] census sample: {len(sample)} contracts "
          f"({len(poor[:10])} no-amount, {len(rich[:6])} with-amount) x the endpoints a run calls")

    per_contract: list[dict[str, Any]] = []
    error_names: dict[str, dict[str, int]] = {}
    # Sample-scoped on purpose: the global `shapes` counter also sees the id map's
    # dead-tail batches, which are expected no-rows answers and would swamp it.
    sample_shapes = {"no-rows": 0, "http-4xx": 0, "http-5xx": 0, "transport": 0}

    def attempt(label: str, path: str, body: dict) -> tuple[dict[str, Any], int]:
        """One call, tallied into error_names/sample_shapes the way try_request
        would tally it into failed_calls."""
        res = call(path, body)
        if res.get("failed"):
            name = res.get("ErrorName") or res.get("error") or f"http {res.get('http')}"
            bucket = error_names.setdefault(label, {})
            bucket[str(name)[:60]] = bucket.get(str(name)[:60], 0) + 1
            shape = res.get("shape") or ("transport" if res.get("error") else "unknown")
            sample_shapes[shape] = sample_shapes.get(shape, 0) + 1
        return res, 1 if res.get("failed") else 0

    for ident in sample:
        # Exact replay of app/aquira/client.py:load_contract's probe order.
        failed_reads = 0
        months_res, fail = attempt("detail-analysis", "/Contract/GetContractDetailAnalysis",
                                   {"ID": ident, "id": ident, "RevenueDateType": 0, "name": "detail"})
        failed_reads += fail
        months = normalize_revenue_months(months_res.get("payload")) if months_res.get("payload") else []
        lines: list[Any] = []
        if not months:
            spot_res, fail = attempt("spot-lines", "/Contract/GetSpotLineDetailAnalysis",
                                     {"id": ident, "ID": ident, "name": "spot-lines"})
            failed_reads += fail
            lines = normalize_spot_lines(spot_res.get("payload")) if spot_res.get("payload") else []
            if not lines:
                ls_res, fail = attempt("loadspotline", "/Contract/LoadSpotline",
                                       {"ContractID": ident, "name": "spotline"})
                failed_reads += fail
                lines = normalize_spot_lines(ls_res.get("payload")) if ls_res.get("payload") else []
            charge_res, fail = attempt("detail-analysis(charge)", "/Contract/GetContractDetailAnalysis",
                                       {"ID": ident, "id": ident, "RevenueDateType": 0, "name": "detail"})
            failed_reads += fail
            charges = normalize_charge_lines(charge_res.get("payload")) if charge_res.get("payload") else []
            lines = [*lines, *charges]
        load_res = call(f"/Contract/Load/{ident}", {"name": "load"})
        load_ok = not load_res.get("failed")
        per_contract.append({
            "id": ident, "amount": ident in rich_set, "failed_reads": failed_reads,
            "months": len(months), "lines": len(lines), "load_ok": load_ok,
        })
        stratum = "has-amount" if ident in rich_set else "no-amount "
        print(f"           id {ident:>4} {stratum}: failed_reads={failed_reads} "
              f"months={len(months)} lines={len(lines)} "
              f"Contract/Load={'ok' if load_ok else 'FAILED'}")

    print("\n  --- what failed, by endpoint (this is what failed_calls would contain) ---")
    for label, bucket in sorted(error_names.items()):
        print(f"  [INFO] {label}: {bucket}")
    if not error_names:
        print("  [OK ] no sampled optional read failed — the 84 comes from somewhere else; "
              "paste the /ui/logs event payload (it carries the first 8 failed_calls) instead.")

    poor_cost = sum(c["failed_reads"] for c in per_contract if not c["amount"])
    poor_n = sum(1 for c in per_contract if not c["amount"])
    rich_cost = sum(c["failed_reads"] for c in per_contract if c["amount"])
    rich_n = sum(1 for c in per_contract if c["amount"])
    rate_poor = poor_cost / poor_n if poor_n else 0.0
    rate_rich = rich_cost / rich_n if rich_n else 0.0
    predicted = round(rate_poor * len(poor) + rate_rich * len(rich))
    print(f"  [INFO] cost per contract: no-amount={rate_poor:.2f} failed read(s), "
          f"with-amount={rate_rich:.2f}")
    print(f"  [INFO] PREDICTED failed_reads for all {len(live)} contracts = {predicted} "
          f"(the last run reported 84 — compare, and check the strata counts above against "
          f"164/84 from the tail section)")
    empty_records = sum(1 for c in per_contract if not c["months"] and not c["lines"])
    print(f"  [INFO] sampled contracts with NO revenue data at all: {empty_records}/{len(per_contract)} "
          f"-> extrapolates to ~{round(len(live) * empty_records / max(1, len(per_contract)))} of {len(live)}")

    # /User/Lookup overhead: load_sales_reps tries two bodies; a 404 on the first is
    # harmless but is also counted as a failed read.
    for body in ({"salesReps": True, "CurrentOnly": True, "SearchTerm": ""}, {"salesReps": True}):
        res = call("/User/Lookup", body)
        print(f"  [INFO] /User/Lookup {json.dumps(body)} -> http={res.get('http')} "
              f"rows={res.get('rows')} failed={res.get('failed')} "
              f"ErrorName={res.get('ErrorName')!r}")

    total_failures = sum(sample_shapes.values())
    print(f"  [INFO] failure shapes over the sampled revenue reads: {sample_shapes} "
          f"(all calls incl. the id map: {shapes})")
    if not total_failures:
        print("  [VERDICT] no optional revenue read failed on any sampled contract, so the run's 84 "
              "failed_reads are NOT coming from load_contract's probe path. The /ui/logs event "
              "payload is the only thing that can name them — paste it.")
    elif sample_shapes["no-rows"] == total_failures:
        print(f"  [VERDICT] all {total_failures} sampled failures were HTTP 200 + Success:false — this "
              "API's spelling of 'no rows', the same convention that made the SearchByID tail "
              "unprovable. `certified = ... and not self.failed_calls` therefore counts an EMPTY "
              "contract as a read failure, so this tenant can never earn pruning while losing no "
              "data at all. The fix belongs in _integrity: separate no-rows/optional failures from "
              "completeness-critical ones. Do not retry, and do not blame Aquira.")
    else:
        print(f"  [VERDICT] the sampled failures MIX shapes: {sample_shapes}. http-5xx/transport ones "
              "genuinely SHOULD uncertify the pull; no-rows ones should not. Split them in "
              "_integrity by shape, not by endpoint, and keep this section's per-endpoint tally as "
              "the regression check.")
    print("  [NEXT] Paste this section AND the <pre> payload from /ui/logs (the WARN "
          "'Revenue pruning suppressed' event stores the first 8 failed_calls, method + path + "
          "error each). Whether the fix is 'stop counting no-rows' or 'repair endpoint X' depends "
          "on those 8.")
    try:
        client.delete("/Session/Delete")
    except Exception:
        pass
    client.close()


# --------------------------------------------------------------------------
# STATUS DOMAIN: which IncludeStatuses codes exist and what they mean
# --------------------------------------------------------------------------
def probe_statuses() -> None:
    """/Contract/Lookup accepts statuses 0-5 today, but normalize.py only labels
    0-3 (Draft/Proposal/Booked/Cancelled); every other code falls through to
    IsProposal=True, so an Inactive/Expired proposal keeps stage 'proposal' in
    HubSpot forever. This maps code -> count -> what normalize currently derives,
    so the stage mapping can be fixed with facts, not guesses."""
    s = _settings()
    base = (s.aquira_base_url or "").rstrip("/")
    if not (base and s.aquira_username and s.aquira_password):
        print("STATUSES: aquira credentials required")
        return
    client = httpx.Client(base_url=base, timeout=TIMEOUT)
    login = client.post(
        "/Session/Post", json={"Username": s.aquira_username, "Password": s.aquira_password}
    ).json()
    if not login.get("Success", True):
        print("  [FAIL] login refused")
        client.close()
        return
    print("\n=== CONTRACT STATUS DOMAIN (reads only) ===")

    from app.aquira.normalize import normalize_contract

    # --- ground-truth records the business already characterized in the UI ---
    # Each entry is (identifier as the human sees it, UI state). Resolving every
    # one through SearchByID, Load, CD-search and the sweep's /Contract/Search
    # shows (a) whether these are internal IDs or ContractCDs, and (b) the exact
    # API fields that carry Active/Submitted — so the stage map is derived from
    # these known states, not guessed. STATUS_FIELDS must be updated once the
    # active/submitted field names are confirmed live.
    GROUND_TRUTH = [
        ("1328", "Inactive - Unsubmitted proposal"),
        ("1316", "Inactive - Unsubmitted proposal"),
        ("1310", "Inactive - Submitted proposal"),
        ("1219", "Inactive - Submitted proposal"),
        ("1330", "Active - Unsubmitted proposal"),
        ("1320", "Active - Unsubmitted proposal"),
        ("1325", "Active - Submitted proposal"),
        ("1305", "Active - Submitted proposal"),
        ("1334", "Active contract"),
        ("1275", "Active contract"),
        ("1329", "Inactive contract"),
        ("1306", "Inactive contract"),
        ("1070", "UNKNOWN - an older fixture claimed booked; verify in the UI"),
    ]
    def _post(path: str, body: dict):
        try:
            r = client.request("POST", path, json=body)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {}
        except Exception:
            return None, {}

    import re as _re

    _STATUSISH = _re.compile(r"(?i)active|status|submit|sent|current|archiv|cancel|delet|approv|book|state|flag|probab|valid|build")

    def _flat(entity: dict) -> dict:
        """Scalar status-ish fields, unwrapping FieldValue {Value:...} shapes."""
        out: dict[str, Any] = {}
        for key, val in entity.items():
            if not _STATUSISH.search(str(key)):
                continue
            if isinstance(val, dict) and "Value" in val:
                val = val.get("Value")
            if val is None or isinstance(val, (str, int, float, bool)):
                out[str(key)] = val
        return out

    print("\n  --- ground-truth records: how is each UI state encoded (Search row AND Load entity)? ---")
    search_seen: dict[str, set[str]] = {}
    load_seen: dict[str, set[str]] = {}
    for ident, meaning in GROUND_TRUTH:
        s_status, s_payload = _post("/Contract/Search", {
            "SearchTerm": ident, "IncludeActive": True, "IncludeInactive": True,
        })
        s_row = None
        for row in ((s_payload or {}).get("Data") or (s_payload or {}).get("Entity") or []):
            if isinstance(row, dict) and str(row.get("ContractCD") or row.get("CD")) == ident:
                s_row = row
                break
        if s_row is None:
            print(f"  [FAIL] {ident} ({meaning}): no /Contract/Search row with ContractCD={ident}")
            continue
        internal = s_row.get("ID")
        s_flat = _flat(s_row)
        print(f"  [INFO] CD {ident} ({meaning}) -> internal ID {internal!r}  Status={s_row.get('Status')!r}")
        print(f"           SEARCH status-ish: {s_flat}")
        nc_s = normalize_contract(dict(s_row))
        if nc_s:
            print(f"           normalize(Search row): booked={nc_s.get('IsContract')} "
                  f"proposal={nc_s.get('IsProposal')} cancelled={nc_s.get('Cancelled')} status={nc_s.get('Status')!r}")
        for k, v in s_flat.items():
            search_seen.setdefault(k, set()).add(str(v))
        if str(internal).isdigit():
            l_status, l_payload = _post(f"/Contract/Load/{internal}", {"name": "probe"})
            entity = l_payload.get("Entity") if isinstance(l_payload.get("Entity"), dict) else {}
            l_flat = _flat(entity)
            if l_flat:
                print(f"           LOAD   status-ish: {l_flat}")
                nc_l = normalize_contract(l_payload)
                if nc_l:
                    print(f"           normalize(Load):     booked={nc_l.get('IsContract')} "
                          f"proposal={nc_l.get('IsProposal')} cancelled={nc_l.get('Cancelled')} status={nc_l.get('Status')!r}")
                for k, v in l_flat.items():
                    load_seen.setdefault(k, set()).add(str(v))
    print(f"\n  [INFO] SEARCH status-ish keys: {sorted(search_seen)}")
    print(f"  [INFO] LOAD   status-ish keys: {sorted(load_seen)}")
    for k, vals in sorted(search_seen.items()):
        print(f"           search {k}: {sorted(vals)}")
    for k, vals in sorted(load_seen.items()):
        print(f"           load   {k}: {sorted(vals)}")
    print("  [NEXT] Confirm IsActiveFlag (or whichever field) separates Active from Inactive across "
          "the ground-truth rows above, then fix normalize.py STATUS_LABELS to this tenant's real "
          "vocabulary (Status 1=Contract, 2=Proposal-Unsubmitted, 3=Proposal-Submitted) and drive the "
          "inactive-proposal stage from the confirmed active flag.")

    # Any endpoint that lists the status vocabulary outright?
    for path in ("/Contract/LookupStatuses", "/Contract/GetStatuses", "/Status/Lookup", "/Contract/Statuses"):
        try:
            r = client.get(path)
            note = ""
            if r.status_code == 200 and r.content:
                try:
                    data = r.json()
                    rows = data.get("Data") or data.get("Entity") or data
                    note = f" payload={json.dumps(rows, default=str)[:220]}"
                except Exception:
                    note = " (non-JSON)"
            print(f"  [INFO] GET {path:32s} -> HTTP {r.status_code}{note}")
        except Exception as exc:
            print(f"  [INFO] GET {path:32s} -> {type(exc).__name__}")

    accepted: list[int] = []
    stuck: list[tuple[int, int]] = []
    for code in range(0, 20):
        try:
            r = client.post("/Contract/Lookup", json={"SearchTerm": "", "IncludeStatuses": [code]})
            payload = r.json() if r.content else {}
        except Exception as exc:
            print(f"  [FAIL] code {code}: {type(exc).__name__}")
            continue
        rejected = payload.get("Success") is False or payload.get("Error") not in (None, 0, "0")
        rows = payload.get("Data") or payload.get("Entity") or []
        rows = rows if isinstance(rows, list) else []
        if rejected and not rows:
            print(f"  [INFO] code {code:2d}: rejected by the server")
            continue
        accepted.append(code)
        sample = next((row for row in rows if isinstance(row, dict)), {})
        flags = {k: sample.get(k) for k in
                 ("Status", "StatusName", "StatusCode", "Active", "IsActive", "Cancelled", "IsContract", "IsProposal")
                 if isinstance(sample, dict) and k in sample}
        derived = ""
        if sample:
            nc = normalize_contract(dict(sample))
            if nc:
                stage = ("closedlost" if nc.get("Cancelled")
                         else "closedwon" if nc.get("IsContract")
                         else "proposal")
                derived = (f" -> normalize says booked={nc.get('IsContract')} proposal={nc.get('IsProposal')} "
                           f"cancelled={nc.get('Cancelled')} status={nc.get('Status')!r} stage={stage}")
                if stage == "proposal" and code not in (0, 1):
                    stuck.append((code, len(rows)))
        raw_fields = " ".join(f"{k}={v!r}" for k, v in flags.items()) or "(sample lacks status-ish fields)"
        print(f"  [INFO] code {code:2d}: rows={len(rows):4d}  {raw_fields}{derived}")
    if stuck:
        print(f"  [VERDICT] codes {stuck} currently normalize to stage 'proposal' — those are the "
              "inactive proposals still sitting in HubSpot. Tell me the intended meaning of each "
              "code (ask a dispatcher to read the Aquira UI status dropdown) and the stage map "
              "in normalize.py/deal_properties will be fixed to match.")
    print(f"  [INFO] server-accepted status codes: {accepted}")
    try:
        client.delete("/Session/Delete")
    except Exception:
        pass
    client.close()


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    if which in {"all", "aquira"}:
        probe_aquira()
    if which in {"all", "hubspot"}:
        probe_hubspot()
    if which in {"all", "swagger"}:
        probe_swagger()
    if which in {"all", "filters"}:
        probe_filter_fields()
    if which in {"all", "pagination"}:
        probe_pagination()
    if which in {"all", "sharding"}:
        probe_sharding()
    if which in {"all", "ids"}:
        probe_id_sweep()
    if which in {"all", "tail"}:
        probe_sweep_tail()
    if which in {"all", "reads"}:
        probe_read_census()
    if which in {"all", "statuses"}:
        probe_statuses()
    print("\nDone. Paste this whole output back. It contains no credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
