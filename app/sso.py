"""OIDC single sign-on (Entra ID compatible) with group→role mapping.

Flow: auth code + PKCE + state + nonce, confidential client (client_secret).
The id_token is signature-verified against the provider JWKS with issuer,
audience, expiry and nonce checks; the authorization cookie between /sso and
/sso/callback is Fernet-encrypted with the app's own key material.

Group → role, evaluated fresh at every login:
  member of sso_admin_group  -> admin
  member of sso_sales_group  -> sales
  sso groups unset           -> any authenticated account gets sales
                                (Entra app-assignment is then the gate)
  otherwise                  -> refused
When a user is in more than 6 groups Entra replaces the `groups` claim with
an overage marker; we then resolve membership through Graph /memberOf with
the delegated access token (GroupMember.Read.All, admin consented).

The env-configured admin password login and named local users keep working
side by side — an IdP outage must never strand the operator. A `disabled`
AppUser is refused even with a valid Entra token (the in-app kill switch).
"""
from __future__ import annotations

import secrets
import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.settings import get_settings

STATE_TTL_SECONDS = 600
_CACHE_TTL = 3600
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class SsoError(RuntimeError):
    pass


def _http_get(url: str, **kwargs: Any) -> httpx.Response:
    return httpx.get(url, timeout=15.0, **kwargs)


def _http_post(url: str, **kwargs: Any) -> httpx.Response:
    return httpx.post(url, timeout=15.0, **kwargs)


def sso_ready() -> bool:
    settings = get_settings()
    return bool(settings.sso_enabled and settings.oidc_issuer and settings.oidc_client_id)


def _discovery() -> dict[str, Any]:
    settings = get_settings()
    url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    cached = _discovery_cache.get(url)
    if cached and cached[0] > time.time():
        return cached[1]
    try:
        doc = _http_get(url).json()
    except Exception as exc:
        raise SsoError(f"OIDC discovery failed: {exc}") from exc
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not doc.get(field):
            raise SsoError(f"OIDC discovery missing {field}")
    _discovery_cache[url] = (time.time() + _CACHE_TTL, doc)
    return doc


def _signing_key(kid: str) -> Any:
    settings = get_settings()
    cache_key = settings.oidc_issuer
    cached = _jwks_cache.get(cache_key)
    if not cached or cached[0] <= time.time():
        doc = _discovery()
        try:
            jwks = _http_get(doc["jwks_uri"]).json()
        except Exception as exc:
            raise SsoError(f"JWKS fetch failed: {exc}") from exc
        cached = (time.time() + _CACHE_TTL, jwks)
        _jwks_cache[cache_key] = cached
    for entry in (cached[1] or {}).get("keys") or []:
        if entry.get("kid") == kid and entry.get("kty") == "RSA":
            return rsa.RSAPublicNumbers(
                int.from_bytes(_b64url(entry["e"]), "big"),
                int.from_bytes(_b64url(entry["n"]), "big"),
            ).public_key()
    raise SsoError(f"no JWKS key for kid {kid!r}")


def _b64url(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    import base64

    return base64.urlsafe_b64decode(text + pad)


def begin_login() -> tuple[str, str]:
    """Returns (redirect_url, encrypted state cookie value)."""
    settings = get_settings()
    doc = _discovery()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    payload = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "iat": time.time(),
        "redirect_uri": _redirect_uri(),
    }
    from app.runtime import encrypt_value

    cookie = encrypt_value(_json_dumps(payload))
    params = {
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "redirect_uri": payload["redirect_uri"],
        "response_mode": "query",
        "scope": "openid profile email User.Read",
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url_join(_sha256(verifier)),
        "code_challenge_method": "S256",
    }
    url = doc["authorization_endpoint"] + "?" + str(httpx.QueryParams(params))
    return url, cookie


def _redirect_uri() -> str:
    base = (get_settings().public_base_url or "").rstrip("/")
    return f"{base}/ui/sso/callback"


def _sha256(text: str) -> bytes:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).digest()


def _b64url_join(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)


def _json_loads(text: str) -> Any:
    import json

    return json.loads(text)


def decrypt_state(cookie_value: str) -> dict[str, Any] | None:
    from app.runtime import decrypt_value

    raw = decrypt_value(cookie_value)
    if not raw:
        return None
    try:
        payload = _json_loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict) or time.time() - float(payload.get("iat") or 0) > STATE_TTL_SECONDS:
        return None
    return payload


def complete_login(code: str, state: str, cookie_value: str) -> dict[str, str]:
    """Exchanges the code, validates the id_token, maps group → role.
    Returns {email, name, role, subject} or raises SsoError."""
    settings = get_settings()
    stored = decrypt_state(cookie_value)
    if not stored or not stored.get("state") or stored["state"] != state:
        raise SsoError("login state mismatch (expired or tampered)")
    doc = _discovery()
    try:
        response = _http_post(
            doc["token_endpoint"],
            data={
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": stored.get("redirect_uri") or _redirect_uri(),
                "code_verifier": stored.get("verifier") or "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        tokens = response.json()
    except Exception as exc:
        raise SsoError(f"token exchange failed: {exc}") from exc
    raw_id = str(tokens.get("id_token") or "")
    if not raw_id:
        detail = tokens.get("error_description") or tokens.get("error") or "no id_token"
        raise SsoError(f"token endpoint refused: {detail}")
    try:
        header = jwt.get_unverified_header(raw_id)
        claims = jwt.decode(
            raw_id,
            _signing_key(header.get("kid") or ""),
            algorithms=["RS256"],
            audience=settings.oidc_client_id,
            issuer=doc["issuer"],
            leeway=15,
        )
    except SsoError:
        raise
    except Exception as exc:
        raise SsoError(f"id_token validation failed: {exc}") from exc
    if claims.get("nonce") != stored.get("nonce"):
        raise SsoError("nonce mismatch")

    groups = {str(g) for g in (claims.get("groups") or [])}
    if not groups and claims.get("_claim_names"):
        groups = _graph_groups(str(tokens.get("access_token") or ""))

    role = _role_from_groups(groups)
    if role is None:
        raise SsoError("signed in, but not a member of the HQ-Sales or HQ-Admins groups")
    email = str(
        claims.get("preferred_username") or claims.get("email") or claims.get("upn") or ""
    ).strip().lower()
    if not email:
        raise SsoError("token carried no usable email/UPN claim")
    name = str(claims.get("name") or email.split("@", 1)[0])
    return {"email": email, "name": name, "role": role, "subject": str(claims.get("sub") or "")}


def _graph_groups(access_token: str) -> set[str]:
    if not access_token:
        return set()
    ids: set[str] = set()
    url = "https://graph.microsoft.com/v1.0/me/memberOf"
    while url:
        try:
            page = _http_get(url, headers={"Authorization": f"Bearer {access_token}"}).json()
        except Exception as exc:
            raise SsoError(f"Graph group lookup failed: {exc}") from exc
        for entry in page.get("value") or []:
            if str(entry.get("@odata.type") or "").endswith("#microsoft.group"):
                ids.add(str(entry.get("id") or ""))
        url = str(page.get("@odata.nextLink") or "") or None
    return ids


def _role_from_groups(groups: set[str]) -> str | None:
    settings = get_settings()
    admin = settings.sso_admin_group.strip()
    sales = settings.sso_sales_group.strip()
    if not admin and not sales:
        return "sales"  # Entra app assignment is the gate; local role defaults to sales
    if admin and admin in groups:
        return "admin"
    if sales and sales in groups:
        return "sales"
    return None
