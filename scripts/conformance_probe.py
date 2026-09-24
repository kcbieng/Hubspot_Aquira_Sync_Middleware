"""Read-only conformance probe for HubQuira.

Run this locally where .env holds the real credentials:

    python scripts/conformance_probe.py aquira
    python scripts/conformance_probe.py hubspot
    python scripts/conformance_probe.py pagination
    python scripts/conformance_probe.py sharding
    python scripts/conformance_probe.py ids
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
    if which in {"all", "statuses"}:
        probe_statuses()
    print("\nDone. Paste this whole output back. It contains no credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
