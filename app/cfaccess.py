"""Cloudflare Zero Trust (Access) identity for HubQuira.

The tunnel authenticates against Entra ID and asserts the winning identity to this
origin in a JWT signed with the team's keys. We **verify that signature** rather than
trusting ``Cf-Access-Authenticated-User-Email``, which is a plain header: the compose
file also publishes the app port on the host, so anything able to reach :8080 directly
could otherwise name itself admin and be believed.

Everything stays dark until all three settings are present (``cf_access_enabled``,
``cf_access_team_domain``, ``cf_access_aud_tag``) — an under-configured install must not
silently become "anyone with a header is admin", and cookie login keeps working so an
Access or Entra outage never strands the operator.

Entra group -> role reuses ``sso_admin_group`` / ``sso_sales_group``, because Access
forwards the same ``groups`` claim the OIDC token carried — but only if the Access
identity provider was configured to send it. Cloudflare does not add IdP groups on its
own, so with those ids set and the claim missing, every Access sign-in is refused; the
refusal is logged under its own cause so it is not misread as a bad audience tag.

The local account controls still apply: an AppUser is JIT-provisioned on first sight,
and ``disabled`` (the in-app kill switch) and ``role_locked`` are honored. That check is
cached for IDENTITY_TTL_SECONDS so rendering a page doesn't hit the DB per request,
which means revoking someone takes up to that long to take effect.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import jwt
from starlette.requests import Request

from app.settings import get_settings
from app.sso import role_from_groups

logger = logging.getLogger(__name__)

JWKS_PATH = "/cdn-cgi/access/certs"
JWKS_TTL_SECONDS = 3600  # Cloudflare rotates team keys; re-resolve the client hourly
IDENTITY_TTL_SECONDS = 60  # how long a disabled/role change takes to bite
_CLOCK_LEEWAY = 15

_jwks_clients: dict[str, tuple[float, Any]] = {}
_identities: dict[str, tuple[float, dict[str, str] | None]] = {}


class CfAccessError(RuntimeError):
    pass


def cf_ready() -> bool:
    settings = get_settings()
    return bool(
        settings.cf_access_enabled
        and str(settings.cf_access_team_domain or "").strip()
        and str(settings.cf_access_aud_tag or "").strip()
    )


def team_issuer() -> str:
    domain = str(get_settings().cf_access_team_domain or "").strip().rstrip("/")
    return domain if domain.startswith("https://") else f"https://{domain.removeprefix('https://')}"


def clear_caches() -> None:
    """Drop cached keys and identity decisions (tests, and after a settings change)."""
    _jwks_clients.clear()
    _identities.clear()


def provider_logout_url() -> str | None:
    """Where someone who signed in through Access actually has to go to get out.

    None means this mode is off and the local cookie is the whole story.
    """
    if not cf_ready():
        return None
    return team_issuer() + "/out"


def _signing_key(token: str) -> Any:
    url = team_issuer() + JWKS_PATH
    cached = _jwks_clients.get(url)
    if cached is None or cached[0] <= time.time():
        from jwt import PyJWKClient

        cached = (time.time() + JWKS_TTL_SECONDS, PyJWKClient(url, cache_keys=True))
        _jwks_clients[url] = cached
    return cached[1].get_signing_key_from_jwt(token).key


def verify_token(token: str) -> dict[str, Any]:
    """Claims from an Access JWT, or CfAccessError.

    The audience check is not optional ceremony: ``aud`` is this application's tag, and
    without it any token minted for any app in the Cloudflare team would open this one.
    """
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            _signing_key(token),
            algorithms=["ES256"],
            audience=str(settings.cf_access_aud_tag or "").strip(),
            issuer=team_issuer(),
            leeway=_CLOCK_LEEWAY,
        )
    except CfAccessError:
        raise
    except Exception as exc:
        raise CfAccessError(f"Access token rejected: {exc}") from exc


def _account_gate(email: str, name: str, role: str, subject: str) -> dict[str, str] | None:
    """Ask the local account store, which owns the kill switch and the role override."""
    cached = _identities.get(email)
    if cached is not None and cached[0] > time.time():
        return cached[1]
    resolved: dict[str, str] | None
    try:
        from app.db.repo import Repo

        repo = Repo()
        try:
            user, allowed = repo.provision_sso_user(email, name, role, subject)
            # Read every attribute here: the commit inside expires them, and touching
            # user.role after close() raises on the detached instance — which the
            # except-block below would silently turn into "refused".
            effective_role = role
            if allowed and user is not None and str(user.role) in {"admin", "sales"}:
                effective_role = str(user.role)
        finally:
            repo.close()
        if not allowed:
            resolved = None
        else:
            resolved = {"email": email, "role": effective_role, "name": name, "source": "cf-access"}
    except Exception as exc:
        # Fail closed: with no account store we cannot confirm this user is not
        # disabled, and "unknown" must not be allowed to mean "admin".
        logger.warning("could not check local account for %s: %s", email, exc)
        resolved = None
    _identities[email] = (time.time() + IDENTITY_TTL_SECONDS, resolved)
    return resolved


def identity_from_request(request: Request) -> dict[str, str] | None:
    """Identity asserted by Cloudflare Access, or None.

    None covers "disabled", "no header", "bad token", and "groups don't authorize this
    account" — and each of those simply falls through to the existing cookie session, so
    the env-configured admin stays reachable when Cloudflare or Entra is the thing that
    is broken. A token that verifies but whose groups are not mapped is logged, because
    that is an Access policy wider than the app's own role map.
    """
    settings = get_settings()
    if not cf_ready():
        return None
    token = str(request.headers.get(settings.cf_access_jwt_header) or "").strip()
    if not token:
        return None
    try:
        claims = verify_token(token)
    except CfAccessError as exc:
        logger.info("rejected a Cloudflare Access token: %s", exc)
        return None
    email = str(claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "").strip().lower()
    if not email:
        logger.info("Access token verified but carries no email claim; ignoring it")
        return None
    raw_groups = claims.get("groups")
    if raw_groups is None and role_from_groups(set()) is None:
        # Cloudflare does not forward IdP groups unless the identity provider was
        # configured to send `groups` as a custom OIDC claim. Without it nobody can be
        # authorized, and "nobody can sign in" looks identical to a bad aud tag unless
        # it is named.
        logger.warning(
            "Access token for %s carries no `groups` claim: add `groups` as a custom OIDC "
            "claim on the Access identity provider, or clear the group ids to let Access's own "
            "policy be the gate",
            email,
        )
        return None
    role = role_from_groups({str(group) for group in (raw_groups or [])})
    if role is None:
        logger.info("access token for %s matched no role group", email)
        return None
    name = str(claims.get("commonname") or claims.get("name") or email.split("@", 1)[0])
    return _account_gate(email, name, role, str(claims.get("sub") or ""))
