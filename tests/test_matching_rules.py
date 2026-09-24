"""Ordered, admin-configurable matching rules: engine semantics, repo CRUD,
build_plan consumption, and the admin page."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db_mod
from app.main import app
from app.mapping.matching import apply_match_rules, values_match
from app.db.repo import Repo
from app.sync.orchestrator import SyncOrchestrator, empty_existing


def _repo(tmp_path) -> Repo:
    engine = create_engine(f"sqlite:///{tmp_path / 'rules.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    return Repo(session=sessionmaker(bind=engine)())


def _rule(name, conditions, on_match="link"):
    return {"id": hash(name) % 1000, "name": name, "on_match": on_match, "conditions": conditions}


def _cond(aq, hs, mode):
    return {"aquira_field": aq, "hubspot_field": hs, "mode": mode}


# ---------------------------------------------------------------------------
# mode truth table
# ---------------------------------------------------------------------------
def test_values_match_modes():
    assert values_match("domain", "https://WWW.acme.com/x", "acme.com")
    assert not values_match("domain", "acme", "acme.com")
    assert values_match("normalized", "Acme Holdings, Inc.", "The ACME Holdings")
    assert not values_match("normalized", "LLC", "LLC")  # junk guard survives rules
    assert values_match("phone", "(214) 555-0100", "+12145550100")
    assert values_match("exact", "  Jane@X.com ", "jane@x.com")
    assert values_match("email", "a@b.co", "A@B.CO")
    assert values_match("contains", "Comcast Advertising Southeast", "Comcast")
    assert not values_match("contains", "ab", "ab")  # below length floor
    assert not values_match("bogus-mode", "x", "x")


# ---------------------------------------------------------------------------
# engine semantics
# ---------------------------------------------------------------------------
CLIENT = {"ID": 5, "Name": "Zeta Widgets", "ShortName": "ZW", "Website": "zeta.com", "Phone": "214-555-0100"}
COMPANY = {"id": "hs-5", "properties": {"name": "Zeta Widget Company", "domain": "other.com", "phone": "2145550100"}}


def test_first_matching_rule_in_order_wins():
    veto = _rule("human check first", [_cond("Phone", "phone", "phone")], "suggest")
    auto = _rule("domain auto", [_cond("Website", "domain", "domain")], "link")
    links, suggestions = apply_match_rules([CLIENT], [COMPANY], [veto, auto])
    assert links == {}
    assert len(suggestions) == 1
    assert "human check first" in suggestions[0]["reason"]  # the FIRST matching rule decided


def test_and_semantics_within_a_rule():
    strict = _rule("name and domain", [_cond("Name", "name", "normalized"), _cond("Website", "domain", "domain")])
    links, _ = apply_match_rules([CLIENT], [COMPANY], [strict])
    assert links == {}  # domain differs -> rule cannot fire
    loose = _rule("phone only", [_cond("Phone", "phone", "phone")])
    links, _ = apply_match_rules([CLIENT], [COMPANY], [loose])
    assert links == {"5": ("hs-5", "phone only")}


def test_one_candidate_cannot_be_linked_twice():
    rule = _rule("phone", [_cond("Phone", "phone", "phone")])
    twin = dict(CLIENT, ID=6)
    links, _ = apply_match_rules([CLIENT, twin], [COMPANY], [rule])
    assert list(links.keys()) == ["5"]  # deterministic: first item takes it


def test_resolved_item_never_also_becomes_a_suggestion():
    # once a higher-priority link rule decides, lower suggest rules stay silent
    exact_name = {"id": "hs-5", "properties": {"name": "Zeta Widgets", "phone": "2145550100"}}
    link = _rule("name equals", [_cond("Name", "name", "normalized")], "link")
    suggest = _rule("phone hint", [_cond("Phone", "phone", "phone")], "suggest")
    links, suggestions = apply_match_rules([CLIENT], [exact_name], [link, suggest])
    assert links == {"5": ("hs-5", "name equals")}
    assert suggestions == []


# ---------------------------------------------------------------------------
# repo: seeding, ordering, CRUD
# ---------------------------------------------------------------------------
def test_repo_seeds_once_and_orders_by_priority(tmp_path):
    repo = _repo(tmp_path)
    assert repo.ensure_default_match_rules() == 6
    assert repo.ensure_default_match_rules() == 0
    names = [r.name for r in repo.list_match_rules("company")]
    assert names == ["Website domain", "Business name + phone", "Business name"]
    active = repo.active_match_rules("company")
    assert [a["name"] for a in active] == names
    repo.update_match_rule(active[0]["id"], enabled=False)
    assert [a["name"] for a in repo.active_match_rules("company")] == names[1:]


def test_repo_move_and_delete(tmp_path):
    repo = _repo(tmp_path)
    repo.ensure_default_match_rules()
    rules = repo.list_match_rules("company")
    repo.move_match_rule(rules[2].id, "up")  # swap ranks 3 and 2
    names = [r.name for r in repo.list_match_rules("company")]
    assert names == ["Website domain", "Business name", "Business name + phone"]
    repo.move_match_rule(repo.list_match_rules("company")[0].id, "up")  # already top: no-op
    assert [r.name for r in repo.list_match_rules("company")][0] == "Website domain"
    repo.delete_match_rule(rules[0].id)
    assert "Website domain" not in [r.name for r in repo.list_match_rules("company")]


def test_repo_create_update_and_blank_conditions_keep(tmp_path):
    repo = _repo(tmp_path)
    row = repo.create_match_rule("contact", "Work email domain", [_cond("Email", "email", "contains")], "suggest")
    active = repo.active_match_rules("contact")
    assert active[-1]["name"] == "Work email domain" and active[-1]["on_match"] == "suggest"
    repo.update_match_rule(row.id, conditions=[])  # blank edit must not wipe conditions
    assert repo.active_match_rules("contact")[-1]["conditions"] == [
        {"aquira_field": "Email", "hubspot_field": "email", "mode": "contains"}
    ]
    assert repo.list_match_rules("company") == []  # contacts seeded lazily by the page, not here


# ---------------------------------------------------------------------------
# build_plan consumes rules for both entities
# ---------------------------------------------------------------------------
def test_build_plan_uses_company_rules_over_heuristic():
    rule = _rule("shortname equals name", [_cond("ShortName", "name", "exact")])
    catalog = {"clients": [dict(CLIENT, Name="Zeta", ShortName="Alpha")], "contacts": [], "contracts": [], "reps": []}
    existing = empty_existing()
    existing["companies"] = [{"id": "hs-5", "properties": {"name": "alpha"}}]
    items = SyncOrchestrator().build_plan(catalog, existing, ["companies"], {}, match_rules={"company": [rule]})
    company = next(i for i in items if i.get("entityType") == "company")
    assert company["action"] == "update"
    assert company["hubspotId"] == "hs-5"
    assert company["matchedBy"] == "shortname equals name"


def test_build_plan_uses_contact_rules():
    rule = _rule("person name", [_cond("FirstName", "firstname", "exact"), _cond("LastName", "lastname", "exact")])
    contact = {"ID": 90, "FirstName": "Jane", "LastName": "Doe", "Email": "jane@x.com", "Phone": "", "ClientID": None}
    catalog = {"clients": [], "contacts": [contact], "contracts": [], "reps": []}
    existing = empty_existing()
    existing["contacts"] = [{"id": "c-9", "properties": {"firstname": "Jane", "lastname": "Doe", "email": "j@old.com"}}]
    items = SyncOrchestrator().build_plan(catalog, existing, ["contacts"], {}, match_rules={"contact": [rule]})
    row = next(i for i in items if i.get("entityType") == "contact")
    assert row["hubspotId"] == "c-9"
    assert row["matchedBy"] == "person name"
    # identity merge keeps the HubSpot-side email (non-empty current wins)
    assert row["properties"]["email"] == "j@old.com"


def test_build_plan_without_rules_falls_back_to_heuristic():
    catalog = {"clients": [{"ID": 7, "Name": "Acme Co", "Website": "acme.com"}], "contacts": [], "contracts": [], "reps": []}
    existing = empty_existing()
    existing["companies"] = [{"id": "hs-7", "properties": {"name": "Acme", "domain": "acme.com"}}]
    items = SyncOrchestrator().build_plan(catalog, existing, ["companies"], {}, match_rules=None)
    company = next(i for i in items if i.get("entityType") == "company")
    assert company["hubspotId"] == "hs-7"
    assert company["matchedBy"] == "domain"  # heuristic path, as before


# ---------------------------------------------------------------------------
# admin page
# ---------------------------------------------------------------------------
def test_matching_page_requires_login_and_creates_rules():
    client = TestClient(app)
    anon = client.get("/ui/matching", follow_redirects=False)
    assert anon.status_code == 303
    assert "/ui/login" in anon.headers["location"]

    login = client.post("/ui/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert login.status_code == 303

    page = client.get("/ui/matching")
    assert page.status_code == 200
    assert "matching rules" in page.text
    assert "Website domain" in page.text

    create = client.post(
        "/ui/matching",
        data={  # a real browser sends repeat cond_* keys (getlist handles them); this
            # starlette TestClient collapses list-of-tuples bodies, so one pair here
            "action": "create",
            "entity_type": "company",
            "name": "CD equals domain label",
            "on_match": "suggest",
            "cond_aquira": "ClientCD",
            "cond_hubspot": "industry",
            "cond_mode": "exact",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303
    page = client.get("/ui/matching")
    assert "CD equals domain label" in page.text

    # cleanup: this test shares the app's real database with other tests
    repo = Repo()
    for row in repo.list_match_rules("company"):
        if row.name == "CD equals domain label":
            repo.delete_match_rule(row.id)
