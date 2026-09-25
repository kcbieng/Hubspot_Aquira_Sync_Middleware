"""Pins the Phase 1 fail-closed behaviour: no blank/`{}` overwrite, no pruning
from an uncertified or thin Aquira pull, and a suppressed prune must not turn a
clean run into a failed one."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.aquira.normalize import as_str, normalize_client
from app.sync.orchestrator import SyncContext, SyncOrchestrator, empty_catalog, empty_existing
from app.sync.planner import plan_upsert

# a normal scalar
def test_as_str_unwraps_nested_fieldvalue():
    assert as_str({"Value": {"Value": "100 Main St"}, "Access": 2}) == "100 Main St"


def test_as_str_rejects_empty_dict_residue():
    assert as_str({"Value": {}}) == ""
    assert as_str({"Value": {}, "Valid": True, "Label": "Name", "Access": 1}) == ""
    assert as_str({"Value": []}) == ""
    assert as_str({"Value": {"Value": {}}}) == ""


def test_as_str_residue_does_not_beat_the_fallback():
    assert as_str({"Value": {}}, "FALLBACK") == "FALLBACK"
    assert (as_str({"Value": {}}) or "REAL") == "REAL"


def test_normalize_client_does_not_write_brace_string_as_the_name():
    payload = {
        "Entity": {
            "ID": 5,
            "Fullname": {"Value": {}, "Valid": True, "Label": "Fullname", "Access": 1},
            "Addresses": {"Value": {}, "Valid": True, "Label": "Addresses", "Access": 1},
        }
    }
    client = normalize_client(payload)
    assert client["Name"] != "{}"
    assert client["Name"] == "Client 5"
    assert client["PhysicalAddress"] != "{}"


def test_plan_upsert_withholds_blank_overwrite_of_existing_identity():
    existing = {"hubspotId": "c-1", "properties": {"name": "ACME", "phone": "2145550100"}, "hash": "x"}
    item = plan_upsert("company", "5", "", {"name": "", "phone": "{}", "aquira_id": "5"}, existing)
    assert "name" not in item["properties"]
    assert "phone" not in item["properties"]
    assert item["properties"]["aquira_id"] == "5"
    assert sorted(item["suppressed"]) == ["name", "phone"]
    assert item["warning"] and "Withheld" in item["warning"]


def test_plan_upsert_withholds_zero_over_real_deal_amount():
    existing = {"hubspotId": "d-1", "properties": {"amount": "12000"}, "hash": "x"}
    item = plan_upsert("deal", "9", "C9 — ACME", {"amount": 0, "dealname": "C9 — ACME"}, existing)
    assert "amount" not in item["properties"]
    assert item["suppressed"] == ["amount"]


def test_plan_upsert_allows_first_write_of_a_blank_value():
    item = plan_upsert("company", "5", "", {"name": "", "aquira_id": "5"}, None)
    assert item["action"] == "create"
    assert item["suppressed"] == []


def _catalog_and_existing():
    contracts = [
        {"ID": 1, "ContractCD": "C1", "IsContract": True, "StartDate": "2026-01-01",
         "EndDate": "2026-01-31", "TotalValue": 1000, "lines": []},
        {"ID": 2, "ContractCD": "C2", "IsContract": True, "StartDate": "2026-01-01",
         "EndDate": "2026-01-31", "TotalValue": 500, "lines": [], "_detail_failed": True},
    ]
    catalog = {"clients": [], "contacts": [], "contracts": contracts, "reps": []}
    existing = empty_existing()
    existing["revenue"] = [
        {"id": "r1", "properties": {"aquira_id": "1:2026-01:0", "amount": "1000"}},
        {"id": "r2", "properties": {"aquira_id": "1:2025-12:0", "amount": "500"}},
        {"id": "r3", "properties": {"aquira_id": "2:2025-12:0", "amount": "250"}},
    ]
    return catalog, existing


def _pruned_ids(allow_prune: bool):
    catalog, existing = _catalog_and_existing()
    orchestrator = SyncOrchestrator()
    items = orchestrator.build_plan(
        catalog, existing, ["deals"], {}, aquira_id=None, allow_prune=allow_prune
    )
    return {
        str(item.get("aquiraId"))
        for item in items
        if item.get("action") == "delete-stale"
    }


def test_certified_pull_prunes_stale_months_for_healthy_contracts():
    pruned = _pruned_ids(allow_prune=True)
    assert "1:2025-12:0" in pruned


def test_contract_with_failed_detail_load_is_never_pruned():
    pruned = _pruned_ids(allow_prune=True)
    assert "2:2025-12:0" not in pruned


def test_uncertified_pull_prunes_nothing():
    assert _pruned_ids(allow_prune=False) == set()


def test_detail_failed_marker_never_reaches_hubspot_properties():
    catalog, existing = _catalog_and_existing()
    orchestrator = SyncOrchestrator()
    items = orchestrator.build_plan(catalog, existing, ["deals"], {}, allow_prune=True)
    for item in items:
        assert "_detail_failed" not in (item.get("properties") or {})


def test_suppressed_prune_is_a_notice_and_does_not_fail_the_run():
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=7)
    result = SyncOrchestrator().run(
        SyncContext(trigger="manual", whatif=True),
        repo=repo,
        catalog=empty_catalog(),
        existing=empty_existing(),
    )
    assert result["status"] == "success"
    assert result.get("error") is None
    assert any("pruning suppressed" in notice.lower() for notice in result["notices"])
    assert result["warnings"] == []


def test_certified_pull_emits_no_suppression_notice():
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=8)
    catalog = empty_catalog()
    catalog["_integrity"] = {"certified": True, "failed_reads": 0, "detail_failures": 0}
    result = SyncOrchestrator().run(
        SyncContext(trigger="manual", whatif=True),
        repo=repo,
        catalog=catalog,
        existing=empty_existing(),
    )
    assert result["notices"] == []
    assert result["status"] == "success"


def test_try_request_flags_a_source_pinned_at_the_row_cap():
    from app.aquira.client import AquiraSessionClient

    client = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")
    client.request = lambda *a, **k: {"Success": True, "Data": [{"ID": i} for i in range(100)]}
    assert client.try_request("GET", "/Client/Get") is not None
    assert client.truncated_sources == ["GET /Client/Get"]

    client.truncated_sources = []
    client.request = lambda *a, **k: {"Success": True, "Data": [{"ID": i} for i in range(99)]}
    client.try_request("GET", "/Client/Get")
    assert client.truncated_sources == []


# ---------------------------------------------------------------------------
# ID sweep: SearchByID batches are the only self-certifying enumeration
# ---------------------------------------------------------------------------
def _sweep_client(existing_ids):
    from app.aquira.client import AquiraSessionClient

    client = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")

    def fake(method, path, **kwargs):
        ids = (kwargs.get("json") or {}).get("SearchIDs") or []
        return {"Success": True, "Data": [{"ID": i} for i in ids if i in existing_ids]}

    client.request = fake
    return client


def test_sweep_recovers_everything_past_the_row_cap_and_self_certifies():
    client = _sweep_client(set(range(1, 121)))
    rows, complete = client.sweep_enumerate("Client")
    assert complete is True
    assert len(rows) == 120
    assert client.truncated_sources == []
    assert client.failed_calls == []


def test_sweep_does_not_declare_a_tenant_empty_before_finding_a_single_row():
    # First live ID at 480: the dead-tail window (200 IDs) would stop a naive
    # sweep at ID 200 and certify a completely missed tenant as "empty".
    client = _sweep_client(set(range(480, 501)))
    rows, complete = client.sweep_enumerate("Client")
    assert complete is True
    assert len(rows) == 21


def test_sweep_fails_closed_when_a_response_ignores_the_id_list():
    from app.aquira.client import AquiraSessionClient

    client = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")
    client.request = lambda *a, **k: {"Success": True, "Data": [{"ID": i} for i in range(100)]}
    _, complete = client.sweep_enumerate("Client")
    assert complete is False
    assert any("ignored the ID list" in src for src in client.truncated_sources)


def test_sweep_fails_closed_when_a_batch_errors():
    from app.aquira.client import AquiraSessionClient

    client = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")
    state = {"n": 0}

    def fake(method, path, **kwargs):
        state["n"] += 1
        if state["n"] == 4:  # first batch after the doubling probe
            raise RuntimeError("HTTP 500 boom")
        ids = (kwargs.get("json") or {}).get("SearchIDs") or []
        return {"Success": True, "Data": [{"ID": i} for i in ids if i <= 120]}

    client.request = fake
    _, complete = client.sweep_enumerate("Client")
    assert complete is False
    assert client.failed_calls
    assert any("unproven" in src for src in client.truncated_sources)


def _aquira_tenant_client(existing_ids, vanishing=()):
    """A fake that speaks this tenant's real SearchByID envelope, measured live
    2026-09-24: HTTP 200 + Success:true + ErrorName:"None" (the enum's name for no
    error) when at least one requested id exists, and Success:false +
    ErrorName:"NotFound" + Error:-12 when the batch matches NOTHING. request()
    translates the latter into a raise, which is what used to break the sweep.
    `vanishing` ids stop existing after the opening doubling probe.
    """
    from app.aquira.client import AquiraApiError, AquiraSessionClient

    client = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")
    existing = set(existing_ids)
    calls = {"n": 0}

    def fake(method, path, **kwargs):
        ids = [int(i) for i in (kwargs.get("json") or {}).get("SearchIDs") or []]
        calls["n"] += 1
        if calls["n"] > 1:
            existing.difference_update(vanishing)
        hits = sorted(set(ids) & existing)
        if not hits:
            raise AquiraApiError("no match", error=-12, error_name="NotFound")
        return {"Success": True, "ErrorName": "None", "Data": [{"ID": i} for i in hits]}

    client.request = fake
    return client


def test_sweep_certifies_the_tail_on_a_tenant_that_errors_on_zero_match_batches():
    # THE production bug: every full run was PARTIAL, both resources, at the first
    # fully-dead id range, because a zero-match batch is an error rather than an empty
    # list. The sentinel id in each batch makes zero-match unreachable, so the dead
    # tail is provable again and no call has to be forgiven.
    live = set(range(1, 280)) - {17, 42, 99, 150, 200, 231, 240, 260}
    client = _aquira_tenant_client(live)
    rows, complete = client.sweep_enumerate("Client")
    assert complete is True
    assert {row["ID"] for row in rows} == live
    assert client.truncated_sources == []
    assert client.failed_calls == []


def test_sweep_does_not_count_the_sentinel_row_as_a_live_hit():
    # ids 1..60, anchor 32. Every tail batch echoes id 32; counting that echo as a hit
    # would pin empty_run at 0 forever, so a 60-row tenant would burn 400 batches and
    # then be reported PARTIAL.
    client = _aquira_tenant_client(set(range(1, 61)))
    rows, complete = client.sweep_enumerate("Client")
    assert complete is True
    assert len(rows) == 60


def test_sweep_fails_closed_when_the_sentinel_is_deleted_mid_run():
    # With the sentinel gone, a dead range is indistinguishable from a broken endpoint.
    # PARTIAL is the only honest verdict — and what was already found is still returned.
    client = _aquira_tenant_client(set(range(1, 280)), vanishing={256})
    rows, complete = client.sweep_enumerate("Client")
    assert complete is False
    assert any("unproven" in src for src in client.truncated_sources)
    assert len(rows) == 278


def test_sweep_fails_closed_when_the_server_answers_ids_it_was_not_given():
    # SearchByID is not row-capped (400 ids -> 279 rows on the live tenant), so the
    # >=100-rows tripwire cannot be the integrity check. Returned ids must be a subset
    # of requested ids or the sweep cannot self-certify at any batch size.
    from app.aquira.client import AquiraSessionClient

    client = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")

    def fake(method, path, **kwargs):
        # Never honors the id list: answers one id that was not requested, on every
        # call including the opening doubling probe. The probe is validated for the
        # same reason — an unchecked stray anchor would become a sentinel that absorbs
        # every later stray.
        return {"Success": True, "ErrorName": "None", "Data": [{"ID": 424242}]}

    client.request = fake
    _, complete = client.sweep_enumerate("Client")
    assert complete is False
    assert any("not requested" in src for src in client.truncated_sources)


def test_unnormalizable_sweep_rows_are_reported_and_uncertify(monkeypatch):
    import app.aquira.client as aquira_client

    monkeypatch.setattr(aquira_client, "normalize_client", lambda payload: None)
    client = aquira_client.AquiraSessionClient(
        base_url="http://aquira.invalid", username="u", password="p"
    )
    client.sweep_enumerate = lambda resource: ([{"ID": 1}, {"ID": 2}], True)
    clients, _ = client.enumerate_clients()
    assert clients == []
    assert any("did not normalize" in src for src in client.truncated_sources)


def test_completed_sweeps_certify_the_pull_and_partial_sweeps_do_not():
    from app.aquira.client import AquiraSessionClient

    client = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")
    client.logged_in = True
    client.request = lambda *a, **k: {"Success": True, "Data": []}
    client.enumerate_clients = lambda: ([], True)
    client.enumerate_contracts = lambda: ([], True)
    catalog = client.load_catalog()
    assert catalog["_integrity"]["certified"] is True

    client2 = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")
    client2.logged_in = True
    client2.request = lambda *a, **k: {"Success": True, "Data": []}
    client2.enumerate_clients = lambda: ([], True)
    client2.enumerate_contracts = lambda: ([{"ID": 7, "ContractCD": "C7"}], False)
    catalog2 = client2.load_catalog()
    assert catalog2["_integrity"]["certified"] is False
    assert catalog2["_integrity"]["enumeration"]["contracts_complete"] is False


# ---------------------------------------------------------------------------
# Certification: "nothing here" is an answer, not a failed read
# ---------------------------------------------------------------------------
def _revenue_tenant_client(monthed_ids, *, analysis_fails=None, contacts_found=False, stated_values=None):
    """A client speaking this tenant's per-record read shapes, measured live 2026-09-24.

    /Contract/GetContractDetailAnalysis answers Success:true with an EMPTY Data.Items
    for a contract that has no booked revenue, so an empty contract is not an error.
    /Client/LookupContacts answers a client with no contacts as Success:false +
    ErrorName:"NotFound" (Error -12) — the envelope that used to break the sweep.
    Only /Contract/LoadSpotline answered NotFound for every contract, including ones
    holding 657 spots, because its spec body needs a SpotlineID; `seen` proves it is
    no longer called.
    """
    from app.aquira.client import AquiraApiError, AquiraSessionClient

    client = AquiraSessionClient(base_url="http://aquira.invalid", username="u", password="p")
    client.logged_in = True
    seen: list[str] = []

    def fake(method, path, **kwargs):
        seen.append(path)
        if path.startswith("/Client/Load/"):
            return {"Success": True, "ErrorName": "None", "Entity": {"ID": 21, "Name": "Acme"}}
        if path == "/Client/LookupContacts":
            if contacts_found:
                return {"Success": True, "ErrorName": "None", "Data": [{"ID": 5, "Name": "Jane"}]}
            raise AquiraApiError("NotFound", error=-12, error_name="NotFound", status_code=200)
        if path.startswith("/Contract/Load/"):
            cid = int(path.rsplit("/", 1)[-1])
            return {"Success": True, "ErrorName": "None", "Entity": {
                "ID": cid, "ContractCD": f"C{cid}", "Status": 1, "AccountID": 21,
                "StartDate": "2026-01-01", "EndDate": "2026-03-01"}}
        if path == "/Contract/GetContractDetailAnalysis":
            if analysis_fails:
                raise AquiraApiError(
                    f"Aquira POST {path} failed (HTTP {analysis_fails})", status_code=analysis_fails
                )
            cid = (kwargs.get("json") or {}).get("ID")
            items = (
                [{"StationShortName": "KFM", "Year": 2026, "Month": 1, "NetAmount": 1000.0}]
                if cid in monthed_ids
                else []
            )
            return {"Success": True, "ErrorName": "None", "Data": {"Items": items}}
        if path == "/Contract/GetSpotLineDetailAnalysis":
            return {"Success": True, "ErrorName": "None", "Data": {"Items": []}}
        if path == "/User/Lookup":
            return {"Success": True, "ErrorName": "None", "Data": []}
        raise AssertionError(f"unexpected read {method} {path}")

    client.request = fake
    client.enumerate_clients = lambda: ([{"ID": 21, "Name": "Acme"}], True)
    client.enumerate_contracts = lambda: (
        [
            {"ID": cid, "ContractCD": f"C{cid}", "AccountID": 21, "TotalValue": (stated_values or {}).get(cid, 0)}
            for cid in (1, 2)
        ],
        True,
    )
    return client, seen


def test_a_contract_with_no_revenue_does_not_uncertify_the_pull():
    # 84 of this tenant's 248 contracts have no monthly revenue. Each one used to end
    # the read chain at /Contract/LoadSpotline, whose NotFound answer counted as pull
    # damage — so pruning could never be earned, on any run, at any cost.
    client, seen = _revenue_tenant_client({1})
    catalog = client.load_catalog()
    integrity = catalog["_integrity"]
    assert "/Contract/LoadSpotline" not in seen
    assert integrity["contract_rows"] == 2
    assert integrity["revenue_rows"] == 1
    assert integrity["certified"] is True
    assert integrity["critical_reads"] == 0


def test_a_client_with_no_contacts_is_reported_but_not_blamed():
    # The lookup answers this API's "no rows" envelope; it is tenant shape, so it is
    # counted as absent and cannot hold certification.
    client, _ = _revenue_tenant_client({1, 2})
    integrity = client.load_catalog()["_integrity"]
    assert integrity["absent_reads"] == 1
    assert integrity["failed_reads"] == 1
    assert integrity["critical_reads"] == 0
    assert integrity["certified"] is True
    assert integrity["failed_calls"][0]["shape"] == "absent"
    assert integrity["failed_calls"][0]["error_name"] == "NotFound"


def test_a_5xx_on_an_optional_read_still_uncertifies():
    # Forgiving empty answers must not forgive an outage: the same endpoint failing
    # server-side is critical whatever the caller declared optional.
    client, _ = _revenue_tenant_client({1}, analysis_fails=500)
    integrity = client.load_catalog()["_integrity"]
    assert integrity["critical_reads"] > 0
    assert integrity["certified"] is False
    critical = [call for call in integrity["failed_calls"] if call["shape"] == "critical"]
    assert critical and all(call["http"] == 500 for call in critical)


def test_a_pull_where_no_contract_has_revenue_is_never_certified():
    # What a dead analysis endpoint looks like, and the shape must stay uncertified
    # even though every read answered politely: pruning from it would clear every
    # revenue_period record in HubSpot.
    client, _ = _revenue_tenant_client(set(), contacts_found=True)
    integrity = client.load_catalog()["_integrity"]
    assert integrity["contract_rows"] == 2
    assert integrity["revenue_rows"] == 0
    assert integrity["critical_reads"] == 0
    assert integrity["certified"] is False


def test_the_all_empty_backstop_does_not_uncertify_a_targeted_run():
    # A targeted sync of one genuinely line-less contract is a legitimate shape: the
    # backstop guards a tenant-wide outage, and a targeted run prunes nothing outside
    # its own scope anyway.
    client, _ = _revenue_tenant_client(set(), contacts_found=True)
    client.resolve_clients = lambda query: [{"ID": 21, "Name": "Acme"}]
    client.search_contracts = lambda query: [{"ID": 1, "ContractCD": "C1", "AccountID": 21}]
    integrity = client.load_catalog(aquira_id="1")["_integrity"]
    assert integrity["contract_rows"] == 1
    assert integrity["revenue_rows"] == 0
    assert integrity["certified"] is True


def test_the_row_cap_sentinel_ignores_per_record_detail_reads():
    # Reading the Items envelope means the row-cap sentinel can finally COUNT these
    # answers — and one contract's spot log legitimately holds 657 rows (measured
    # live). Applying the 100-row enumeration cap to a per-record detail read would
    # report a complete answer as truncation and withhold certification again.
    client, _ = _revenue_tenant_client(set())
    client.load_catalog()
    assert client.truncated_sources == []

    airings = [
        {"StationShortName": "KFM", "SpotDate": "2026-01-06T00:00:00", "SpotLineID": 460, "Duration": 30, "Rate": 12.0}
        for _ in range(657)
    ]
    spot_client, _ = _revenue_tenant_client(set())
    original = spot_client.request

    def fake(method, path, **kwargs):
        if path == "/Contract/GetSpotLineDetailAnalysis":
            return {"Success": True, "ErrorName": "None", "Data": {"Items": airings}}
        return original(method, path, **kwargs)

    spot_client.request = fake
    spot_client.load_catalog()
    assert spot_client.truncated_sources == []


def test_a_657_row_spot_log_still_certifies_end_to_end():
    # The same tenant fact at the level the operator sees: a big spot log must not
    # block pruning, and must not invent 657 revenue lines out of airing-grain rows.
    airings = [
        {"StationShortName": "KFM", "SpotDate": "2026-01-06T00:00:00", "SpotLineID": 460, "Duration": 30, "Rate": 0.0}
        for _ in range(657)
    ]
    client, _ = _revenue_tenant_client({1})
    original = client.request

    def fake(method, path, **kwargs):
        if path == "/Contract/GetSpotLineDetailAnalysis":
            return {"Success": True, "ErrorName": "None", "Data": {"Items": airings}}
        return original(method, path, **kwargs)

    client.request = fake
    catalog = client.load_catalog()
    integrity = catalog["_integrity"]
    assert integrity["truncated_sources"] == []
    assert integrity["certified"] is True
    for row in catalog["contracts"]:
        assert len(row.get("lines") or []) < 657


def test_a_contract_stating_money_with_no_lines_is_held_from_pruning():
    # The hold that replaces the deleted guess: if the sweep row says money and no
    # revenue line came back, the record is not empty — our read is. Pruning it would
    # delete the deal's periods, so it is kept out of prune scope instead.
    client, _ = _revenue_tenant_client(set(), stated_values={1: 12000.0})
    catalog = client.load_catalog()
    by_id = {int(row["ID"]): row for row in catalog["contracts"]}
    assert by_id[1].get("_detail_failed") is True
    assert by_id[2].get("_detail_failed") is False
    assert catalog["_integrity"]["detail_failures"] == 1

    # And the marker is honored where it matters: the held contract's stale periods
    # are out of scope, the other one's are not.
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=12)
    existing = empty_existing()
    existing["revenue"] = [
        {"id": "r1", "properties": {"aquira_id": "1:2025-12:0", "amount": "6000"}},
        {"id": "r2", "properties": {"aquira_id": "2:2025-12:0", "amount": "500"}},
    ]
    existing["deals"] = [
        {"id": "d1", "properties": {"aquira_id": "1"}},
        {"id": "d2", "properties": {"aquira_id": "2"}},
    ]
    catalog["_integrity"] = {"certified": True, "contract_rows": 2, "revenue_rows": 1}
    items = SyncOrchestrator().build_plan(catalog, existing, ["deals"], {}, allow_prune=True)
    pruned = {str(item.get("aquiraId")) for item in items if item.get("action") == "delete-stale"}
    assert pruned == {"2:2025-12:0"}


def test_suppression_notice_blames_critical_reads_and_separates_empty_ones():
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=11)
    catalog = empty_catalog()
    catalog["_integrity"] = {
        "certified": False,
        "failed_reads": 85,
        "critical_reads": 1,
        "absent_reads": 84,
        "failed_calls": [{"method": "POST", "path": "/Contract/Get", "shape": "critical"}]
        + [{"method": "POST", "path": "/Client/LookupContacts", "shape": "absent"}] * 84,
        "truncated_sources": [],
        "detail_failures": 0,
        "contract_rows": 248,
        "revenue_rows": 164,
    }
    result = SyncOrchestrator().run(
        SyncContext(trigger="manual", whatif=True),
        repo=repo,
        catalog=catalog,
        existing=empty_existing(),
    )
    text = " ".join(result["notices"])
    assert "1 failed read(s)" in text
    assert "84 read(s) answered 'no rows'" in text
    # The log payload must lead with the call that actually matters, not with 84
    # copies of a harmless empty answer.
    warn_call = [c for c in repo.add_event.call_args_list if c.args[1] == "WARN"][-1]
    detail = warn_call.args[3]
    assert [call["path"] for call in detail["failed_calls"]] == ["/Contract/Get"]


# ---------------------------------------------------------------------------
# Field ownership: human pipeline moves survive; source transitions propagate
# ---------------------------------------------------------------------------
def _booked_contract():
    return {"ID": 9, "ContractCD": "C9", "IsContract": True, "StartDate": "2026-01-01",
            "EndDate": "2026-01-31", "TotalValue": 12000, "lines": []}


def _deal_existing(stage="appointmentscheduled"):
    return {
        "9": {
            "hubspotId": "d-9",
            "hash": "stale-hash",
            "properties": {
                "dealname": "Keep me",
                "dealstage": stage,
                "pipeline": "default",
                "amount": "12000",
                "aquira_id": "9",
            },
        }
    }


def _plan_deal(existing, snapshots):
    from app.sync.planner import plan_deals

    items = plan_deals([_booked_contract()], existing, {}, None, snapshots)
    return items[0]


def test_existing_deal_without_a_snapshot_never_yanks_a_human_stage():
    item = _plan_deal(_deal_existing(), None)
    props = item["properties"]
    assert "dealstage" not in props and "pipeline" not in props and "dealname" not in props
    assert props["amount"]  # money is still Aquira-owned and written
    assert item["preserved"] == ["dealname", "dealstage", "pipeline"]


def test_aquira_transition_pushes_the_stage_even_past_a_human_move():
    snapshot = {
        "9": {
            "aquira": {"dealstage": "proposal", "pipeline": "default", "dealname": "C9 — Contract"},
            "hubspot": {},
        }
    }
    item = _plan_deal(_deal_existing(), snapshot)
    # The rep moved it to appointmentscheduled, but Aquira booked the contract:
    # the derived stage changed since our last write, so the board follows the money.
    assert item["properties"].get("dealstage") == "closedwon"
    assert "dealname" in item["preserved"]  # name did not change at the source


def test_rep_revert_after_a_push_is_respected_until_the_source_moves_again():
    snapshot = {
        "9": {
            "aquira": {"dealstage": "closedwon", "pipeline": "default", "dealname": "C9 — Contract"},
            "hubspot": {"dealstage": "closedwon"},
        }
    }
    item = _plan_deal(_deal_existing(stage="closedlost"), snapshot)
    assert "dealstage" not in item["properties"]
    assert "dealstage" not in [d["field"] for d in item["diffs"]]  # the revert will not be reverted


# ---------------------------------------------------------------------------
# 3-way writeback: HubSpot-only edits flow to Aquira, both-side edits conflict
# ---------------------------------------------------------------------------
def _writebacks(hs_name, aq_name, snapshot):
    from app.sync.planner import plan_identity_writebacks

    companies = [{"aquira_id": "5", "hubspotId": "c-5", "properties": {"name": hs_name}}]
    clients = {"5": {"ID": 5, "Name": aq_name, "Phone": "", "Website": "", "PhysicalAddress": ""}}
    return plan_identity_writebacks(companies, clients, {"5": snapshot} if snapshot else None)


def test_writeback_flows_only_the_hubspot_side_move():
    items = _writebacks("HS-EDIT", "OLD", {"hubspot": {"name": "OLD"}, "aquira": {"Name": "OLD"}})
    assert len(items) == 1
    assert items[0]["properties"] == {"Name": "HS-EDIT"}


def test_writeback_stays_silent_when_only_aquira_moved():
    items = _writebacks("OLD", "AQ-FRESH", {"hubspot": {"name": "OLD"}, "aquira": {"Name": "OLD"}})
    assert items == []


def test_writeback_conflict_writes_neither_side():
    items = _writebacks("HS-EDIT", "AQ-EDIT", {"hubspot": {"name": "OLD"}, "aquira": {"Name": "OLD"}})
    assert len(items) == 1
    item = items[0]
    assert item["action"] == "skip"
    assert item["properties"] == {}
    assert item["conflicts"] == ["Name"]
    assert "BOTH" in item["warning"]


def test_snapshot_roundtrip_in_the_repo(tmp_path):
    import app.db as db_mod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'snap.db'}", future=True)
    db_mod.Base.metadata.create_all(engine)
    from app.db.repo import Repo

    repo = Repo(session=sessionmaker(bind=engine)())
    assert repo.get_snapshots() == {}
    repo.save_snapshot("deal", "9", {"amount": "12000"}, {"dealstage": "closedwon"})
    snap = repo.get_snapshots()
    assert snap["deal"]["9"]["aquira"]["dealstage"] == "closedwon"
    repo.save_snapshot("deal", "9", {"amount": "13000"}, {"dealstage": "closedwon"})  # upsert
    assert repo.get_snapshots()["deal"]["9"]["hubspot"]["amount"] == "13000"


# ---------------------------------------------------------------------------
# Archive-on-vanished: only certified full runs, and only archive (recoverable)
# ---------------------------------------------------------------------------
def _deals_plan(existing_deals, contracts, allow_prune: bool, aquira_id=None):
    orchestrator = SyncOrchestrator()
    catalog = {"clients": [], "contacts": [], "contracts": contracts, "reps": []}
    existing = empty_existing()
    existing["deals"] = existing_deals
    return orchestrator.build_plan(catalog, existing, ["deals"], {}, aquira_id=aquira_id, allow_prune=allow_prune)


_VANISHED = [{"id": "d-999", "properties": {"aquira_id": "999", "dealname": "Ghost deal"}}]
_HERE = [{"ID": 1, "ContractCD": "C1", "IsContract": True, "StartDate": "2026-01-01",
          "EndDate": "2026-01-31", "TotalValue": 1000, "lines": []}]


def test_uncertified_run_archives_nothing():
    items = _deals_plan(_VANISHED, _HERE, allow_prune=False)
    assert not [i for i in items if i.get("action") == "archive"]


def test_targeted_run_never_archives_even_when_certified():
    items = _deals_plan(_VANISHED, _HERE, allow_prune=True, aquira_id="1")
    assert not [i for i in items if i.get("action") == "archive"]


def test_certified_full_run_archives_vanished_deals():
    items = _deals_plan(_VANISHED, _HERE, allow_prune=True)
    archived = [i for i in items if i.get("action") == "archive"]
    assert len(archived) == 1
    assert archived[0]["hubspotId"] == "d-999"
    assert archived[0]["aquiraId"] == "999"


def test_archive_action_calls_hubspot_archive_not_delete():
    hubspot = MagicMock()
    result = SyncOrchestrator().apply_item(
        {"entityType": "deal", "aquiraId": "999", "hubspotId": "d-999", "action": "archive", "properties": {}},
        None,
        hubspot,
        {},
    )
    hubspot.archive.assert_called_once_with("deals", "d-999")
    hubspot.upsert_crm.assert_not_called()
    assert result["action"] == "archive"


# ---------------------------------------------------------------------------
# Status vocabulary: tenant ground truth (2026-09-18, 12 UI-labeled records)
# ---------------------------------------------------------------------------
def test_status_1_is_a_booked_contract_not_a_proposal():
    from app.aquira.normalize import normalize_contract

    row = normalize_contract({"Entity": {"ID": 101, "ContractCD": "1334", "Status": 1}})
    assert row["IsContract"] is True
    assert row["IsProposal"] is False
    assert row["Cancelled"] is False
    assert row["Status"] == "Booked"


def test_status_2_and_3_are_proposals_never_cancelled():
    from app.aquira.normalize import normalize_contract

    draft = normalize_contract({"Entity": {"ID": 102, "ContractCD": "1328", "Status": 2}})
    assert draft["IsProposal"] is True and draft["IsContract"] is False and draft["Cancelled"] is False
    assert draft["Status"] == "Proposal — Unsubmitted"
    sent = normalize_contract({"Entity": {"ID": 103, "ContractCD": "1310", "Status": 3}})
    assert sent["IsProposal"] is True and sent["Cancelled"] is False
    assert sent["Status"] == "Proposal — Submitted"


def test_explicit_flags_still_beat_status_codes():
    from app.aquira.normalize import normalize_contract

    row = normalize_contract({"Entity": {"ID": 104, "Status": 3, "IsContract": True}})
    assert row["IsContract"] is True and row["IsProposal"] is False


def test_inactive_proposal_closes_lost_and_inactive_contract_stays_won():
    from app.aquira.normalize import normalize_contract
    from app.sync.planner import deal_properties

    dead_proposal = normalize_contract({
        "ID": 256, "ContractCD": "1310", "Status": 3, "IsActiveFlag": False,
        "Advertiser": {"ID": 5, "Name": "Dead Co"}, "TotalValue": 500,
    })
    assert dead_proposal["IsProposal"] is True
    assert dead_proposal["IsActive"] is False
    assert dead_proposal["Status"] == "Proposal — Submitted (Inactive)"
    dead_props = deal_properties(dead_proposal)
    assert dead_props["dealstage"] == "closedlost"
    assert dead_props["aquira_is_active"] is False

    inactive_contract = normalize_contract({
        "ID": 272, "ContractCD": "1329", "Status": 1, "IsActiveFlag": False,
        "Advertiser": {"ID": 5, "Name": "Old Flight"}, "TotalValue": 900,
    })
    assert inactive_contract["IsContract"] is True
    assert inactive_contract["Status"] == "Booked (Inactive)"
    assert deal_properties(inactive_contract)["dealstage"] == "closedwon"  # never un-win booked revenue


def test_thin_load_without_isactiveflag_cannot_revive_an_inactive_record():
    from app.aquira.normalize import merge_contract, normalize_contract

    search_row = normalize_contract({"ID": 271, "ContractCD": "1328", "Status": 2, "IsActiveFlag": False, "TotalValue": 100})
    loaded = normalize_contract({"Entity": {"ID": 271, "Status": 2, "Description": {"Value": "detail"}}})  # no flag, as live Load behaves
    assert loaded["IsActive"] is None
    merged = merge_contract(search_row, loaded)
    assert merged["IsActive"] is False
    assert merged["Description"] == "detail"  # the Load still enriches everything it knows


def test_reactivating_a_proposal_reopens_the_stage_as_a_source_transition():
    from app.sync.planner import deal_properties
    from app.aquira.normalize import normalize_contract

    dead = normalize_contract({"ID": 265, "ContractCD": "1320", "Status": 2, "IsActiveFlag": False, "TotalValue": 1})
    revived = normalize_contract({"ID": 265, "ContractCD": "1320", "Status": 2, "IsActiveFlag": True, "TotalValue": 1})
    assert deal_properties(dead)["dealstage"] == "closedlost"
    assert deal_properties(revived)["dealstage"] == "proposal"
    # Ownership logic: the Aquira-derived stage differs from the last-written
    # derived stage, so the transition-push reopens the deal automatically.


# ---------------------------------------------------------------------------
# Inactive == Aquira's delete: archive deals, hold booked periods, purge
# proposal periods, and unarchive on reactivation instead of duplicating.
# ---------------------------------------------------------------------------
_INACTIVE_CONTRACT = {"ID": 2, "ContractCD": "1329", "IsContract": True, "IsActive": False,
                      "StartDate": "2025-01-01", "EndDate": "2025-06-30", "TotalValue": 500, "lines": []}
_INACTIVE_PROPOSAL = {"ID": 3, "ContractCD": "1310", "IsProposal": True, "IsContract": False, "IsActive": False,
                      "StartDate": "2025-01-01", "EndDate": "2025-03-31", "TotalValue": 300, "lines": []}


def test_inactive_deals_are_archived_and_never_upserted():
    orchestrator = SyncOrchestrator()
    catalog = {"clients": [], "contacts": [], "contracts": [_INACTIVE_CONTRACT, _INACTIVE_PROPOSAL], "reps": []}
    existing = empty_existing()
    existing["deals"] = [
        {"id": "d-2", "properties": {"aquira_id": "2", "dealname": "1329 — Old"}},
        {"id": "d-3", "properties": {"aquira_id": "3", "dealname": "1310 — Dead letter"}},
    ]
    items = orchestrator.build_plan(catalog, existing, ["deals"], {}, allow_prune=True)
    archived = {str(i.get("aquiraId")) for i in items if i.get("action") == "archive"}
    written = {str(i.get("aquiraId")) for i in items if i.get("action") in {"create", "update"}}
    assert archived == {"2", "3"}
    assert written == set()  # never write against a record we are archiving


def test_uncertified_run_archives_no_inactive_deals():
    orchestrator = SyncOrchestrator()
    catalog = {"clients": [], "contacts": [], "contracts": [_INACTIVE_PROPOSAL], "reps": []}
    existing = empty_existing()
    existing["deals"] = [{"id": "d-3", "properties": {"aquira_id": "3"}}]
    items = orchestrator.build_plan(catalog, existing, ["deals"], {}, allow_prune=False)
    assert not [i for i in items if i.get("action") == "archive"]


def test_inactive_contract_periods_held_while_inactive_proposal_periods_purged():
    orchestrator = SyncOrchestrator()
    catalog = {"clients": [], "contacts": [], "contracts": [_INACTIVE_CONTRACT, _INACTIVE_PROPOSAL], "reps": []}
    existing = empty_existing()
    existing["revenue"] = [
        {"id": "r2", "properties": {"aquira_id": "2:2025-12:0", "deal_aquira_id": "2", "amount": "500"}},
        {"id": "r3", "properties": {"aquira_id": "3:2025-12:0", "deal_aquira_id": "3", "amount": "300"}},
    ]
    items = orchestrator.build_plan(catalog, existing, ["revenue"], {}, allow_prune=True)
    pruned = {str(i.get("aquiraId")) for i in items if i.get("action") == "delete-stale"}
    assert "3:2025-12:0" in pruned   # proposal periods saved away are junk: purge
    assert "2:2025-12:0" not in pruned  # booked history stays for modelling
    upserts = [i for i in items if i.get("action") in {"create", "update"}]
    assert upserts == []  # no period writes for deactivated records at all


def test_reactivation_unarchives_the_existing_deal_instead_of_creating_a_twin():
    from app.sync.planner import plan_deals

    existing = {
        "9": {
            "hubspotId": "d-9",
            "properties": {"aquira_id": "9", "dealstage": "proposal"},
            "hash": "h",
            "archived": True,
        }
    }
    items = plan_deals([_booked_contract()], existing, {}, None, {})
    assert items[0]["action"] == "unarchive"

    hubspot = MagicMock()
    hubspot.upsert_crm.return_value = {"id": "d-9"}
    applied = SyncOrchestrator().apply_item(dict(items[0]), None, hubspot, {})
    hubspot.restore.assert_called_once_with("deals", "d-9")
    hubspot.upsert_crm.assert_called_once()
    assert applied["action"] == "update"
    assert applied["unarchived"] is True


def test_already_archived_deals_are_not_archived_twice():
    orchestrator = SyncOrchestrator()
    catalog = {"clients": [], "contacts": [], "contracts": [_INACTIVE_PROPOSAL], "reps": []}
    existing = empty_existing()
    existing["deals"] = [{"id": "d-3", "properties": {"aquira_id": "3"}, "archived": True}]
    items = orchestrator.build_plan(catalog, existing, ["deals"], {}, allow_prune=True)
    assert not [i for i in items if i.get("action") == "archive"]


def test_row_cap_hit_reports_the_missing_records_to_the_operator():
    repo = MagicMock()
    repo.add_run.return_value = MagicMock(id=9)
    catalog = empty_catalog()
    catalog["_integrity"] = {
        "certified": False,
        "failed_reads": 0,
        "detail_failures": 0,
        "contract_rows": 100,
        "truncated_sources": ["GET /Contract/Get", "POST /Contract/Search"],
    }
    result = SyncOrchestrator().run(
        SyncContext(trigger="manual", whatif=True),
        repo=repo,
        catalog=catalog,
        existing=empty_existing(),
    )
    text = " ".join(result["notices"])
    assert "row cap" in text
    assert "invisible" in text
    assert "100 contract(s)" in text
    assert result["status"] == "success"
