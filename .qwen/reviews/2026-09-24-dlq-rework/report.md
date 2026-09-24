# Code Review — local uncommitted changes (DLQ rework + deal-stage mapping)

**Date:** 2026-09-24 · **Effort:** high (9 parallel dimension agents + orchestrator verification) · **Verdict:** **REQUEST_CHANGES**
**Machine-readable findings:** `findings.json` beside this file.
**Method limitation:** the bundled `qwen review` CLI could not dispatch its `review` subcommands in this VS Code-extension-only install, so the skill's deterministic gates (capture plan, check-coverage certificate, compose-review) did not run; coverage was certified by dimension receipts + executed probes/mutants instead. Suite state: **268 passed** — the defects below are interleavings and configs the suite never drives (13 targeted mutants confirm it).

Scope reviewed: 15 modified tracked files + 3 untracked new files (`app/jobs/reconcile.py`, `app/ui/templates/stages.html`, `tests/test_stage_mapping.py`). Not reviewed: nothing (no uncoverable chunks); repo-root `1601-*Voltair*` files and `.qwen/tmp` excluded as non-work.

---

## Critical (9) — fix before commit/merge

| # | Where | Defect | Key evidence |
|---|-------|--------|--------------|
| R1-1 | repo.py:283 | Create gate blocks only `frozen` rows → a half-succeeded create (POST commits at `aquira/client.py:722-745`, reload then raises; `item["aquiraId"]` assigned only after return at orchestrator.py:684) is retried as a fresh create → **duplicate Aquira clients** (deactivate-not-delete). Also regresses the invariant still stated at planner.py:756-757; deploy backfill disarms the gate for historical rows. | code trace + migration probe (`create_blocked: set()` on a legacy row) |
| R1-2 | stages.html:78 | **DOM XSS**: `innerHTML` concat of HubSpot-supplied stage `id`/`label`, `fill()` on load; portal-write → HubQuira-admin same-origin script (POST /ui/settings, /ui/users). All Jinja/tojson paths are correctly escaped — only this sink. | file read |
| R1-3 | reconcile.py:59,62 | **`whatif=False` hardcoded** — the only live-write path ignoring the global plan-only interlock (compose default `WHATIF=true`, README, banner "PLAN ONLY"); poll/webhooks/UI all honor it; UI manual-live additionally requires typing `WRITE`. | grep across all enqueue sites |
| R1-4 | orchestrator.py:1021 | Any non-`error` action (incl. `skip`) appends a resolve-pair → DLQ rows auto-close as "written by sync #N" **without the failing field ever being pushed** (field-ownership pops stage fields for deals without snapshots — every deal in plan-only history). | code trace of the line added by this diff |
| R1-5 | repo.py:213+128 | **Attempts double-charged** per cycle (enqueue +1, failure +1) → budget = ~half of `dlq_freeze_after`; common parity freezes inside `add_dead_letter`, which **fires no Teams alert** — contradicting the model docstring's single-alert promise. | interleave probe: froze at pass 2, alert never sent; suite never interleaves |
| R1-6 | orchestrator.py:723 | `stage_map_active` all-or-nothing vs per-token planner substitution: **partial maps** (pipeline `<select>` has no blank option — an untouched Save preselects the custom pipeline; or autodetect misses a token) disable the legacy `ensure_proposal_stage` rescue or inject a default-pipeline stage id into a custom-pipeline write → guaranteed 400 × every deal → DLQ churn. 4 agents converged. | template read + tests cover only full/none maps (mutant survived) |
| R1-7 | routes.py:712 | **Autodetect wipes a working mapping on any transient HubSpot error**: swallows exception → `""`×3 persisted (only `None` skipped) while the form's pipeline id survives → R1-6's broken hybrid; clean-looking 303; no form to recover while HubSpot down; no confirm(). | code trace, persist_settings semantics verified |
| R1-8 | reconcile.py:62 (+ENTITY_TO_SYNC) | **Phantom retries**: revenue_period rows carry the synthetic key `12:2026-01:7` (revenue.py:89) → targeted pull matches nothing → empty run charges the budget → freeze + false Teams escalation. `client` rows route through `writeback`, which `_wanted` discards when `sync_writeback=False` (default) → no-op retries; the freeze then arms the permanent create block (compounds R1-1). | `_wanted` probe: {writeback,companies} → {companies} |
| R1-9 | db/__init__.py:65 + reconcile.py:100 + repo.py:190-201 | **Deploy retry storm + timing defects**: backfill leaves `next_retry_at` NULL = instantly due → first pass enqueues up to 50 groups × full-tenant HubSpot pulls (projection+archived+schema bootstrap each; no 429/backoff anywhere in the client, no `is_busy()` bail, no coalescing; failed enqueues still charged; rows past budget get one final full sync because freeze is computed after enqueue). Postgres sorts NULL **last** (starves those rows; SQLite first). Naive `next_run_time` → first pass ~5 h late, not +2 min. | migration probe (`due-2999: [1,2]`), apscheduler probe (`DRIFT-MINUTES: 302`), perf trace (50 groups × 15-60 calls) |

**One-liner fix set:** restore `status != 'resolved'` gating for unresolved create-failures + fix the apply-guard token test + resolve-pairs only for real `create/update` writes + honor `settings.whatif` (skip, don't charge, in plan-only) + map revenue ids via `_revenue_contract_id` and check the writeback/create gates before enqueue + stagger `next_retry_at` in the backfill (and backfill `hubspot_id` too) with `nulls_first()` + tz-aware `next_run_time` + `is_busy()`/per-pass cap + atomic claim-or-skip + one shared freeze/alert helper charging attempts once at failure observation.

## Suggestion (13)

- **R1-10** repo.py:127 — dedup never backfills newly-known ids; folded rows keep mis-typed NULL `aquira_id` forever.
- **R1-11** repo.py:124 — un-ordered `.first()` over legacy duplicate rows: arbitrary budget winner (plan-dependent on PG).
- **R1-12** db/__init__.py:53 — migration doesn't backfill `hubspot_id` from legacy payload → repeat failures escape dedup (probe: second open row for `hs-9`).
- **R1-13** repo.py:112 — `aid/hid` unclamped vs `String(100)`: overlong ids raise inside the except-pass caller → the failed write is silently **never recorded**.
- **R1-14** repo.py:137 — DLQ settings clamped only at the cadence; `0`/negative → zero backoff or instant freeze; repo.py:131 compares un-`int()`ed (TypeError → swallowed row loss).
- **R1-15** routes.py:773 — "Retry now" means ≤ `dlq_retry_minutes` (web role can't execute); relabel or make the pass wakeable.
- **R1-16** db/__init__.py:50 — `status` Python-default only: rolling-restart inserts land NULL → permanently invisible rows while the badge says "N retrying"; also concurrent-start ALTER race crashes the loser at import.
- **R1-17** worker.py:243 — job blocks duplicated across both entry points; 6 new settings undocumented and cadence never rescheduled.
- **R1-18** repo.py:130 — dedup overwrites `ts` (loses first-failure time); `last_attempt_at` written-never-read; "failing since" unobservable.
- **R1-19** reconcile.py:13 — docstring names a nonexistent button ("Re-open frozen").
- **R1-20** routes.py:704 — `async def` + blocking 30 s HubSpot call stalls the event loop.
- **R1-21** deadletters.html:72 — renders literal "None" for legacy resolved rows.
- **R1-22** tests — mutation-verified holes (13 mutants, all listed): gate filters, wiring, backoff math, NULL ordering, alert count, `whatif`-no-resolve, `persist_settings` DB half, frozen-reset, stage-map glue all unpinnable-green today.

## Needs Human Review (low confidence)

- **R1-23** reconcile.py:66 — *possibly:* `mark_reconciled` writes identity-mapped stale rows; a web-container operator Resolve landing inside the pass window is overwritten and its `resolution` discarded. Fix either way: conditional `UPDATE … WHERE status='open'` (also fixes charged-failed-enqueues).

## Nice to have (2)

- **R1-24** reconcile.py:59 — entity list duplicated inline vs `ENTITY_TO_SYNC["client"]`.
- **R1-25** deadletters.html:7 — post-rename terminology drift (heading/nav/alert/badge) + alert lists ≤6 rows with no "…and N more".

## Verified-clean highlights (so effort isn't re-spent)

Admin gating on all four new routes; `persist_settings` key-allowlist; parameterized ORM only; migration SQL fixed-literals; no credential logging; `|tojson` attribute escaping safe incl. apostrophes; template variables match route contexts; `ENTITY_TO_SYNC` keys == planner `entityType` strings; unmapped stage path byte-identical to old behavior; single scheduler per shipped role (no double-fire as composed); `bump_dead_letter` and `routes.ENTITY_TO_SYNC` fully dead; no positional-limit callers of the changed signature; freeze-alert cannot re-fire per pass (alert-once sound, subject to R1-5's silent path).

## Cross-cutting root causes

1. **The retry budget is an illusion at three points** (R1-5 double-charge, R1-8 phantom targets, R1-9 failed-enqueue charging) — every "attempt" must correspond to one expressible, actually-enqueued write-back plan.
2. **The auto-resolve contract is looser than the cockpit's promise** (R1-4 false resolve, R1-1 half-success identity) — "closed" must mean "this record's write succeeded", nothing weaker.
3. **Deploy-time state transitions were designed for one row at a time, not set-at-once** (R1-9 storm, R1-16 NULL-status, R1-12 dedup escape) — the migration needs a "scheduled but spread out" stamp and id backfill.
4. **Partial configuration states of the stage map are undefined behavior** (R1-6, R1-7) — the save path should own the all-or-none decision; the apply guard should test per token.

---

# Fix pass (same day, 2026-09-24)

All 26 findings applied (outcomes in `findings.json`; the ledger also records **R1-26**, a Critical the cross-file and ops agents flagged and my merge dropped — the worker never re-read the settings overlay, defeating the "fix takes effect next attempt" contract; fixed with `apply_db_overlay()` atop `run()` and `run_reconciliation()`).

**Design shift that resolved several findings at once:** the retry budget is now charged in exactly one place (`mark_reconciled`, per cycle started) and freeze/alerting lives on that one path; rows automation *cannot* express a retry for — failed Aquira creates (duplicate-write risk), client rows while `sync_writeback` is off, rows entering a pass already past budget — are **held** with a visible reason and zero charge instead of burning no-op cycles. The create gate went back to blocking every unresolved create (fold-learning an id, or an operator Resolve, releases it). Reconciliation defers entirely while `whatif` is on, bails when the worker is busy, and caps at 8 enqueues per pass, charging only rows whose sync actually queued.

**Verification:**
- Full suite: **285 passed** (was 268; +17 pins including hold/park/cap/busy/plan-only/overlay-refresh/clamp/backoff-growth/schedule-wiring/deal_pipelines-transform/partial-save-rejection/wipe-prevention/keep-tokens/fold-releases-gate/skip-no-resolve/update-resolves).
- Mutation spot-checks: reverting the create-gate predicate, the resolve-action filter, and the plan-only defer each reddened exactly their pinning tests (4 reds, 24 green mid-check), then reverted clean.
- XSS fix, ts-preservation, and the "+N more" alert are read-verified (no JS/timer harness in this repo; noted honestly in outcomes).

**Deliberate non-fixes (recorded in outcome notes):** no cross-process on-demand retry wake (relabelled instead), no cadence-reschedule hook (restart-required, documented), nav keeps "Dead letters" (mirrors the route), and the import-time migration and template JS stay untested.

**Housekeeping left to you:** `.qwen/tmp/loc.py`, `repro_dlq.py`, `verify_claims.py` are committed debug scratch in HEAD (I deleted them believing they were my agents' throwaway files — restored from git, but they should be `git rm`'d and `.qwen/tmp/` gitignored); the two `1601-*Voltair*` binaries at the repo root remain untracked and unexplained.

