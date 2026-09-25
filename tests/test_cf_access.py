"""Cloudflare Access identity: what a verified token grants, and what a forged one does not.

`_signing_key` is stubbed (no network to Cloudflare's JWKS), so these cover the decision
logic plus PyJWT's own validation of signature, audience, issuer, expiry and algorithm.

Each test that mutates an AppUser uses its own email, so rows created through the JIT
path can't leak a role or a disabled flag into another test.
"""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.requests import Request

from app import cfaccess
from app.api import routes as api_routes
from app.db.models import AppUser
from app.db.repo import Repo
from app.session import COOKIE_NAME, session_identity, session_token
from app.settings import get_settings

TEAM = "firstdallas.cloudflareaccess.com"
AUD = "dddddddd-9999-8888-7777-666666666666"
ADMIN_GROUP = "aaaaaaaa-1111-2222-3333-444444444444"
SALES_GROUP = "bbbbbbbb-1111-2222-3333-444444444444"
UNMAPPED_GROUP = "cccccccc-1111-2222-3333-444444444444"
DEFAULT_EMAIL = "cf-admin@example.com"

_private_key = ec.generate_private_key(ec.SECP256R1())
_public_key = _private_key.public_key()
HEADER = "cf-access-jwt-assertion"


def _enable(monkeypatch, **overrides):
    values = {
        "CF_ACCESS_ENABLED": "true",
        "CF_ACCESS_TEAM_DOMAIN": TEAM,
        "CF_ACCESS_AUD_TAG": AUD,
        "SSO_ADMIN_GROUP": ADMIN_GROUP,
        "SSO_SALES_GROUP": SALES_GROUP,
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    cfaccess.clear_caches()


def _token(**overrides):
    now = int(time.time())
    claims = {
        "aud": [AUD],
        "iss": f"https://{TEAM}",
        "sub": "entra-object-id",
        "email": DEFAULT_EMAIL,
        "commonname": "Ops Person",
        "groups": [ADMIN_GROUP],
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, _private_key, algorithm="ES256", headers={"kid": "test"})


def _authorized(**overrides):
    return session_identity(_request(_token(**overrides)))


def _request(token: str = "", cookies: str = ""):
    headers = []
    if token:
        headers.append((HEADER.encode(), token.encode()))
    if cookies:
        headers.append((b"cookie", cookies.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/ui",
            "raw_path": b"/ui",
            "query_string": b"",
            "scheme": "https",
            "server": ("hubquira.test", 443),
            "root_path": "",
            "client": ("127.0.0.1", 55555),
            "headers": headers,
        }
    )


def _set_account(email: str, **fields) -> None:
    repo = Repo()
    try:
        repo.provision_sso_user(email, "Ops", "admin", "entra-object-id")
        row = repo.session.get(AppUser, email)
        for key, value in fields.items():
            setattr(row, key, value)
        repo.session.commit()
    finally:
        repo.close()
    cfaccess.clear_caches()


@pytest.fixture(autouse=True)
def _offline_jwks(monkeypatch):
    monkeypatch.setattr(cfaccess, "_signing_key", lambda token: _public_key)


def test_role_from_admin_group(monkeypatch):
    # The only door that matters: a token this app verified, for this audience.
    _enable(monkeypatch)
    assert _authorized() == {
        "email": DEFAULT_EMAIL,
        "role": "admin",
        "name": "Ops Person",
        "source": "cf-access",
    }


def test_role_from_sales_group(monkeypatch):
    _enable(monkeypatch)
    assert _authorized(groups=[SALES_GROUP])["role"] == "sales"


def test_the_plain_email_header_grants_nothing(monkeypatch):
    # The reason the JWT is verified rather than read: that header is unsigned, and the
    # compose file also publishes the app port on the host, so a LAN peer could set it
    # to anything.
    _enable(monkeypatch)
    spoofed = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/ui",
            "headers": [(b"cf-access-authenticated-user-email", b"admin@example.com")],
        }
    )
    assert session_identity(spoofed) is None


def test_a_token_minted_for_another_application_is_refused(monkeypatch):
    _enable(monkeypatch)
    assert _authorized(aud=["someone-elses-app-tag"]) is None


def test_an_expired_token_is_refused(monkeypatch):
    _enable(monkeypatch)
    past = int(time.time()) - 3600
    assert _authorized(iat=past, exp=past + 60) is None


def test_a_token_from_another_cloudflare_team_is_refused(monkeypatch):
    _enable(monkeypatch)
    assert _authorized(iss="https://someoneelse.cloudflareaccess.com") is None


def test_the_algorithm_confusion_trick_is_refused(monkeypatch):
    # HS256 signed with the audience tag would verify against a secret the attacker
    # already knows, if the allowed-algorithm list were loose.
    _enable(monkeypatch)
    hs256 = jwt.encode(
        {"aud": [AUD], "iss": f"https://{TEAM}", "email": "atk@example.com", "exp": int(time.time()) + 600},
        AUD,
        algorithm="HS256",
    )
    assert session_identity(_request(hs256)) is None


def test_group_membership_gates_access_even_with_a_valid_token(monkeypatch):
    _enable(monkeypatch)
    assert _authorized(groups=[UNMAPPED_GROUP]) is None


def test_a_token_with_no_groups_claim_at_all_is_refused_when_groups_are_configured(monkeypatch):
    # Cloudflare only embeds IdP groups when `groups` is added as a custom OIDC claim on
    # the Access identity provider, so this is the default state of a fresh setup.
    # Refusing is correct (the configured group ids cannot be honored), and it must not
    # be mistaken for a bad audience tag.
    _enable(monkeypatch)
    claims = _token()
    import jwt as _jwt

    naked = _jwt.decode(claims, _public_key, algorithms=["ES256"], audience=AUD, options={"verify_iss": False})
    naked.pop("groups", None)
    unsigned = _jwt.encode(naked, _private_key, algorithm="ES256")
    assert _authorized() is not None  # control: the same claims WITH groups do authenticate
    assert session_identity(_request(unsigned)) is None


def test_no_groups_claim_still_admits_when_the_operator_relies_on_the_access_policy(monkeypatch):
    # Group ids left blank means "Access's own policy is the gate" — the same
    # convention the OIDC path uses for Entra app assignment.
    _enable(monkeypatch, SSO_ADMIN_GROUP="", SSO_SALES_GROUP="")
    identity = _authorized(groups=[])
    assert identity is not None and identity["role"] == "sales"


def test_a_service_token_authenticates_no_person(monkeypatch):
    # Access service tokens carry common_name and an empty sub, and no email: they are
    # for machines, and must not resolve to a human with a role.
    _enable(monkeypatch)
    service = _token(email="", sub="", common_name="automation.access")
    assert session_identity(_request(service)) is None


def test_unset_role_groups_admit_any_authenticated_account_as_sales(monkeypatch):
    # Same contract as the OIDC path: the Entra app-assignment becomes the gate.
    _enable(monkeypatch, SSO_ADMIN_GROUP="", SSO_SALES_GROUP="")
    identity = _authorized(groups=[UNMAPPED_GROUP])
    assert identity is not None and identity["role"] == "sales"


def test_disabling_the_account_revokes_a_working_token(monkeypatch):
    _enable(monkeypatch)
    email = "cf-revoked@example.com"
    _set_account(email, disabled=True)
    assert _authorized(email=email) is None, "the in-app kill switch must beat a valid token"

    _set_account(email, disabled=False)
    identity = _authorized(email=email)
    assert identity is not None and identity["role"] == "admin"


def test_a_locked_local_role_beats_the_groups_claim(monkeypatch):
    _enable(monkeypatch)
    email = "cf-locked@example.com"
    _set_account(email, role="sales", role_locked=True)
    identity = _authorized(email=email)
    assert identity is not None and identity["role"] == "sales"


def test_the_feature_is_inert_when_disabled(monkeypatch):
    # An under-configured install falls back to cookie login rather than half-trusting
    # headers.
    monkeypatch.delenv("CF_ACCESS_ENABLED", raising=False)
    get_settings.cache_clear()
    cfaccess.clear_caches()
    assert cfaccess.cf_ready() is False
    assert _authorized() is None


def test_all_three_settings_are_required(monkeypatch):
    _enable(monkeypatch, CF_ACCESS_AUD_TAG="")
    assert cfaccess.cf_ready() is False
    assert _authorized() is None


def test_the_cookie_session_still_works_alongside_access(monkeypatch):
    # Break-glass: configuring Access must not remove the operator's way in.
    _enable(monkeypatch)
    identity = session_identity(_request(cookies=f"{COOKIE_NAME}={session_token()}"))
    assert identity is not None and identity["source"] == "config" and identity["role"] == "admin"


def test_an_access_user_reaches_the_console_and_a_forged_header_does_not(monkeypatch):
    # End to end through the real router: this is the property the whole design turns on.
    from fastapi.testclient import TestClient

    from app.main import app

    _enable(monkeypatch)
    client = TestClient(app)
    allowed = client.get("/ui", headers={get_settings().cf_access_jwt_header: _token()})
    assert allowed.status_code == 200, "a verified Access identity must open the console"

    anonymous = client.get("/ui", follow_redirects=False)
    assert anonymous.status_code in (302, 303), "the token, not something else, is what opened the page"

    forged = client.get(
        "/ui",
        headers={"Cf-Access-Authenticated-User-Email": DEFAULT_EMAIL},
        follow_redirects=False,
    )
    assert forged.status_code in (302, 303), "an unsigned header must not be believed"


def test_logout_admits_it_cannot_end_an_access_session(monkeypatch):
    _enable(monkeypatch)
    body = json.loads(api_routes.logout().body)
    assert body["ok"] is True
    assert body["signed_out"] is False, "the header re-authenticates on the very next request"
    assert body["provider_logout_url"] == f"https://{TEAM}/out"

    monkeypatch.delenv("CF_ACCESS_ENABLED", raising=False)
    get_settings.cache_clear()
    cfaccess.clear_caches()
    plain = json.loads(api_routes.logout().body)
    assert plain["signed_out"] is True and plain["provider_logout_url"] is None
