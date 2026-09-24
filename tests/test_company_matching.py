"""Lead-first company matching: auto-link on strong identity, suggest on anything weaker."""

from unittest.mock import MagicMock

from app.mapping.matching import match_clients, normalize_domain, normalize_name, score_client
from app.sync.orchestrator import SyncOrchestrator, empty_existing


def _client(cid, name, website="", phone=""):
    return {"ID": cid, "Name": name, "Website": website or None, "Phone": phone or None}


def _company(hid, name, domain="", website="", phone=""):
    props = {"name": name}
    if domain:
        props["domain"] = domain
    if website:
        props["website"] = website
    if phone:
        props["phone"] = phone
    return {"id": hid, "properties": props}


def test_normalizers():
    assert normalize_domain("HTTPS://www.ACME.com/about?x=1") == "acme.com"
    assert normalize_domain("acme") == ""
    assert normalize_name("The Acme Advertising, Inc.") == "acme advertising"
    assert normalize_name("A&T Co") == "a and t"


def test_domain_wins_even_when_names_differ():
    links, suggestions = match_clients(
        [_client(101, "Acme Advertising", website="https://acme.com/about")],
        [_company("hs-1", "ACME Holdings Ltd", domain="acme.com")],
    )
    assert links == {"101": ("hs-1", "domain")}
    assert suggestions == []


def test_legal_suffix_free_name_matches():
    links, _ = match_clients(
        [_client(102, "Park Cities Baptist Church")],
        [_company("hs-2", "Park Cities Baptist Church, Inc.")],
    )
    assert links == {"102": ("hs-2", "name")}


def test_name_plus_phone_beats_name_alone():
    s1 = score_client(_client(1, "Acme Plumbing"), {"name": "Acme Plumbing"})
    s2 = score_client(_client(1, "Acme Plumbing", phone="(214) 555-0100"), {"name": "Acme Plumbing", "phone": "2145550100"})
    assert s1 == (80, "name")
    assert s2 == (85, "name+phone")


def test_similar_name_is_suggestion_not_link():
    links, suggestions = match_clients(
        [_client(103, "Acme Advertising")],
        [_company("hs-3", "Acme")],
    )
    assert links == {}
    assert len(suggestions) == 1
    assert suggestions[0]["method"] == "similar-name"
    assert suggestions[0]["reason"] == "below-auto-threshold"


def test_phone_only_is_suggestion():
    links, suggestions = match_clients(
        [_client(104, "Bright Signs Co", phone="214-555-0100")],
        [_company("hs-4", "Neon Works", phone="(214) 555-0100")],
    )
    assert links == {}
    assert suggestions[0]["method"] == "phone"


def test_contested_company_links_once_and_suggests_the_loser():
    links, suggestions = match_clients(
        [_client(101, "Acme Agency"), _client(202, "Acme Agency")],
        [_company("hs-1", "Acme Agency")],
    )
    assert links == {"101": ("hs-1", "name")}  # deterministic: lowest client id wins ties
    assert len(suggestions) == 1
    assert suggestions[0]["aquiraId"] == "202"
    assert suggestions[0]["reason"] == "company-already-claimed"


def test_two_companies_one_client_only_the_best_is_linked():
    links, suggestions = match_clients(
        [_client(300, "Acme Brands", website="acme.com")],
        [_company("hs-9", "Unrelated Co"), _company("hs-8", "ACME Inc", domain="acme.com")],
    )
    assert links == {"300": ("hs-8", "domain")}
    assert suggestions == []


def test_junk_short_names_match_nothing():
    links, suggestions = match_clients([_client(1, "LLC")], [_company("h", "LLC")])
    assert links == {} and suggestions == []


# ---------------------------------------------------------------------------
# build_plan integration
# ---------------------------------------------------------------------------
def _acme_catalog():
    return {"clients": [_client(101, "Acme Advertising", website="acme.com")], "contacts": [], "contracts": [], "reps": []}


def _acme_existing():
    existing = empty_existing()
    existing["companies"] = [_company("hs-1", "ACME Advertising Inc", domain="acme.com")]
    return existing


def test_build_plan_links_instead_of_creating():
    items = SyncOrchestrator().build_plan(_acme_catalog(), _acme_existing(), ["companies"], {})
    companies = [i for i in items if i.get("entityType") == "company"]
    assert len(companies) == 1
    item = companies[0]
    assert item["action"] == "update"  # not create: the lead company IS the record
    assert item["hubspotId"] == "hs-1"
    assert item["matchedBy"] == "domain"
    assert item["properties"]["aquira_id"] == "101"
    # HubSpot-wins identity merge: the name sales typed survives the link
    assert item["properties"]["name"] == "ACME Advertising Inc"


def test_already_linked_company_is_never_a_match_candidate():
    existing = _acme_existing()
    existing["companies"][0]["properties"]["aquira_id"] = "777"  # belongs to another Aquira party
    items = SyncOrchestrator().build_plan(_acme_catalog(), existing, ["companies"], {})
    companies = [i for i in items if i.get("entityType") == "company"]
    assert companies[0]["action"] == "create"
    assert "matchedBy" not in companies[0]
    assert not [i for i in items if i.get("entityType") == "match-suggestion"]


def test_linked_lead_is_not_also_created_as_a_new_aquira_client():
    existing = _acme_existing()
    existing["unsynced"] = [dict(existing["companies"][0], aquira_id=None)]
    items = SyncOrchestrator().build_plan(
        _acme_catalog(), existing, ["companies", "writeback"], {}, create_missing_clients=True
    )
    assert not [i for i in items if i.get("entityType") == "client" and i.get("action") == "create"]


def test_suggestions_surface_as_notice_items_with_an_operator_recipe():
    catalog = {"clients": [_client(103, "Acme Advertising Holdings")], "contacts": [], "contracts": [], "reps": []}
    existing = empty_existing()
    existing["companies"] = [_company("hs-3", "Acme")]
    items = SyncOrchestrator().build_plan(catalog, existing, ["companies"], {})
    notices = [i for i in items if i.get("entityType") == "match-suggestion"]
    assert len(notices) == 1
    assert "set that HubSpot record's Aquira ID property to 103" in notices[0]["warning"]
    assert notices[0]["action"] == "notice"


def test_notice_items_apply_nothing():
    hubspot = MagicMock()
    aquira = MagicMock()
    item = {"entityType": "match-suggestion", "action": "notice", "aquiraId": "1", "hubspotId": "h", "properties": {}}
    result = SyncOrchestrator().apply_item(dict(item), aquira, hubspot, {})
    assert result["action"] == "notice"
    hubspot.upsert_crm.assert_not_called()
    hubspot.archive.assert_not_called()
    aquira.update_client_sparse.assert_not_called()


def test_matching_is_skipped_when_companies_not_in_scope():
    items = SyncOrchestrator().build_plan(_acme_catalog(), _acme_existing(), ["contacts"], {})
    assert items == []
