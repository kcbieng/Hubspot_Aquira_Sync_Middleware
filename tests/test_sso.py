"""OIDC SSO against a fake Entra-shaped provider: discovery, PKCE exchange,
id_token validation, group→role mapping, Graph overage, JIT provisioning,
and the /ui/sso routes."""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db_mod
import app.sso as sso
from app.db.models import AppUser, SyncRun, SyncRunItem
from app.db.repo import Repo
from app.main import app

CLIENT_ID = "app-client-id"
ADMIN_GROUP = "aaaaaaaa-1111"
SALES_GROUP = "bbbbbbbb-2222"
ISSUER = "https://idp.test"

_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIV_PEM = _private.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
from jwt.algorithms import RSAAlgorithm  # noqa: E402

_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_private.public_key())
import json as _json  # noqa: E402

_JWK = {**_json.loads(_jwk), "kid": "k1", "use": "sig", "alg": "RS256"}
DOC = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/jwks",
}


def _settings(**over):
    base = dict(
        sso_enabled=True,
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret="s3cr3t",
        sso_admin_group=ADMIN_GROUP,
        sso_sales_group=SALES_GROUP,
        public_base_url="https://hq.example",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _id_token(nonce: str, groups, access_overage: bool = False, **over):
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "obj-42",
        "nonce": nonce,
        "preferred_username": "joe@x.com",
        "name": "Joe Rep",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    if groups is not None:
        claims["groups"] = groups
    if access_overage:
        claims["_claim_names"] = {"groups": "src1"}
    claims.update(over)
    return jwt.encode(claims, _PRIV_PEM, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture()
def fake_provider(monkeypatch):
    sso._discovery_cache.clear()
    sso._jwks_cache.clear()
    state: dict = {"token": None, "graph": None}

    def http_get(url, **kwargs):
        if url.startswith(ISSUER):
            payload = DOC if "well-known" in url else {"keys": [_JWK]}
        elif url.startswith("https://graph.microsoft.com"):
            assert (kwargs.get("headers") or {}).get("Authorization") == "Bearer at-token"
            payload = state["graph"] or {"value": []}
        else:
            raise AssertionError(f"unexpected GET {url}")

        class R:
            def json(self_inner):
                return payload

        return R()

    def http_post(url, **kwargs):
        assert url == DOC["token_endpoint"]
        form = kwargs.get("data") or {}
        assert form.get("grant_type") == "authorization_code"
        assert form.get("client_secret") == "s3cr3t"
        assert form.get("code_verifier")  # PKCE verifier came back
        return SimpleNamespace(json=lambda: {"id_token": state["token"], "access_token": "at-token"})

    monkeypatch.setattr(sso, "get_settings", _settings)
    monkeypatch.setattr(sso, "_http_get", http_get)
    monkeypatch.setattr(sso, "_http_post", http_post)
    return state


def _begin_and_nonce():
    url, cookie = sso.begin_login()
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    return cookie, query["state"][0], query["nonce"][0], url


def test_happy_path_admin_role(fake_provider):
    cookie, state, nonce, url = _begin_and_nonce()
    assert "code_challenge_method=S256" in url and url.startswith(DOC["authorization_endpoint"])
    fake_provider["token"] = _id_token(nonce, [ADMIN_GROUP])
    identity = sso.complete_login("the-code", state, cookie)
    assert identity == {"email": "joe@x.com", "name": "Joe Rep", "role": "admin", "subject": "obj-42"}


def test_sales_group_and_no_group_denied(fake_provider):
    cookie, state, nonce, _ = _begin_and_nonce()
    fake_provider["token"] = _id_token(nonce, [SALES_GROUP])
    assert sso.complete_login("c", state, cookie)["role"] == "sales"
    fake_provider["token"] = _id_token(nonce, ["someone-elses-group"])
    with pytest.raises(sso.SsoError, match="not a member"):
        sso.complete_login("c", state, cookie)


def test_groups_unset_means_assignment_is_the_gate(fake_provider, monkeypatch):
    monkeypatch.setattr(sso, "get_settings", lambda: _settings(sso_admin_group="", sso_sales_group=""))
    cookie, state, nonce, _ = _begin_and_nonce()
    fake_provider["token"] = _id_token(nonce, ["whatever"])
    assert sso.complete_login("c", state, cookie)["role"] == "sales"


def test_group_overage_resolves_via_graph(fake_provider):
    cookie, state, nonce, _ = _begin_and_nonce()
    fake_provider["token"] = _id_token(nonce, None, access_overage=True)
    fake_provider["graph"] = {
        "value": [
            {"@odata.type": "#microsoft.group", "id": ADMIN_GROUP},
            {"@odata.type": "#microsoft.directoryRole", "id": "not-a-group"},
        ]
    }
    assert sso.complete_login("c", state, cookie)["role"] == "admin"


def test_tampered_and_expired_are_refused(fake_provider):
    cookie, state, nonce, _ = _begin_and_nonce()
    fake_provider["token"] = _id_token("different-nonce", [ADMIN_GROUP])
    with pytest.raises(sso.SsoError, match="nonce"):
        sso.complete_login("c", state, cookie)
    fake_provider["token"] = _id_token(nonce, [ADMIN_GROUP])
    with pytest.raises(sso.SsoError, match="state mismatch"):
        sso.complete_login("c", "wrong-state", cookie)
    with pytest.raises(sso.SsoError, match="state mismatch"):
        sso.complete_login("c", state, "not-a-cookie")
    expired = _id_token(nonce, [ADMIN_GROUP], exp=int(time.time()) - 60)
    fake_provider["token"] = expired
    with pytest.raises(sso.SsoError):
        sso.complete_login("c", state, cookie)


def test_unknown_kid_and_audience_mismatch(fake_provider):
    cookie, state, nonce, _ = _begin_and_nonce()
    fake_provider["token"] = jwt.encode(
        {"iss": ISSUER, "aud": "other-client", "sub": "x", "nonce": nonce, "exp": int(time.time()) + 60},
        _PRIV_PEM,
        algorithm="RS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(sso.SsoError):
        sso.complete_login("c", state, cookie)


def test_jit_provision_respects_kill_switch_and_role_lock(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jit.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    repo = Repo(session=sessionmaker(bind=engine)())
    user, allowed = repo.provision_sso_user("joe@x.com", "Joe", "admin", "obj-42")
    assert allowed and user.role == "admin" and user.sso_subject == "obj-42"
    user.disabled = True
    repo.session.commit()
    _, allowed = repo.provision_sso_user("joe@x.com", "Joe", "admin", "obj-42")
    assert allowed is False
    other, allowed = repo.provision_sso_user("sue@x.com", "Sue", "sales", "obj-7")
    other.role_locked = True
    repo.session.commit()
    still_sales, allowed = repo.provision_sso_user("sue@x.com", "Sue", "admin", "obj-7")
    assert allowed and still_sales.role == "sales"  # lock held despite Entra admin group


def _isolate(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr("app.db.repo.SessionLocal", sessionmaker(bind=engine))


def test_ui_sso_routes(fake_provider, tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = TestClient(app)
    page = client.get("/ui/login").text
    assert "Sign in with Microsoft" in page

    start = client.get("/ui/sso", follow_redirects=False)
    assert start.status_code == 302
    assert start.headers["location"].startswith(DOC["authorization_endpoint"])
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(start.headers["location"]).query)
    state, nonce = query["state"][0], query["nonce"][0]
    fake_provider["token"] = _id_token(nonce, [SALES_GROUP])
    done = client.get(f"/ui/sso/callback?code=abc&state={state}", follow_redirects=False)
    assert done.status_code == 303
    assert done.headers["location"] == "/ui"
    assert client.get("/ui").status_code == 200
    row = Repo().get_user("joe@x.com")
    assert row is not None and row.role == "sales" and row.sso_subject == "obj-42"


def test_sso_start_falls_back_when_disabled(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(sso, "sso_ready", lambda: False)
    client = TestClient(app)
    response = client.get("/ui/sso", follow_redirects=False)
    assert response.status_code == 303
    assert "/ui/login" in response.headers["location"]
    assert "Sign in with Microsoft" not in client.get("/ui/login").text


# ---------------------------------------------------------------------------
# record history lookup
# ---------------------------------------------------------------------------
def _seed_history(repo: Repo):
    run = SyncRun(id=1, trigger="manual", whatif=False, status="success")
    repo.session.add(run)
    repo.session.add(
        SyncRunItem(
            run_id=1, entity_type="deal", aquira_id="49", hubspot_id="d-1", action="update",
            diff_json='{"name":"1070 — Christmas", "diffs": [{"field": "amount", "from": "1000", "to": "12500"}], "matchedBy": "Website domain"}',
        )
    )
    repo.session.add(
        SyncRunItem(
            run_id=1, entity_type="company", aquira_id="55", hubspot_id="c-2", action="create",
            diff_json='{"name": "Park Cities Baptist", "diffs": []}',
        )
    )
    repo.session.commit()


def test_search_history_filters(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hist.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    repo = Repo(session=sessionmaker(bind=engine)())
    _seed_history(repo)
    by_aquira = repo.search_history("49")
    assert len(by_aquira) == 1 and by_aquira[0][0].entity_type == "deal"
    assert by_aquira[0][1].trigger == "manual"  # joined to run
    assert len(repo.search_history("Park Cities")) == 1  # name fragment via diff json
    assert len(repo.search_history("", "company")) == 1
    assert repo.search_history("does-not-exist") == []


def test_records_page_lists_history(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = Repo()
    _seed_history(repo)
    client = TestClient(app)
    client.post("/ui/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    page = client.get("/ui/records?q=49").text
    assert "1070 — Christmas" in page
    assert "amount" in page and "12500" in page and "Website domain" in page
