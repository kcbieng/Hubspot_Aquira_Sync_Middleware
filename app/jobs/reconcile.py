"""Dead-letter reconciliation.

Pending failed writes are retried as FRESH targeted syncs through the normal
worker queue — never as replays of the stored payload. That is what makes a
retry honor "updated settings/logic": a deal that failed on a bad stage id
before /ui/stages was configured succeeds on the next reconciliation pass
because the sync re-plans it with the current mapping, current ownership
rules, and current data. The pass therefore re-reads the DB settings overlay
first — this job runs long after boot, and the page promises that a settings
fix takes effect on the very next attempt.

Budget model: `attempts` counts reconciliation CYCLES started for a record.
mark_reconciled is the only thing that charges, and the only freeze path — so
one cycle is never billed twice because the retried sync failed and re-logged
the row, and every freeze raises the Teams escalation exactly once.

Rows that cannot be retried automatically are HELD (frozen with a visible
reason) instead of burning cycles on no-op syncs:
- failed Aquira client CREATES: POST /Client/Create can commit the client and
  still raise mid-reload, so a blind "retry" duplicates a master-system record
  (Aquira deactivates, it does not delete). The create gate holds unresolved
  create rows until a human marks the row Resolved.
- linked client rows while sync_writeback is off: the writeback pass is where
  those write, and reconciliation would just plan an empty companies pass.
While settings.whatif is on the pass defers entirely: plan-only mode is the
repo-wide write-stop switch, and this job must not be the one path that
ignores it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.settings import get_settings

logger = logging.getLogger(__name__)

ENTITY_TO_SYNC: dict[str, list[str]] = {
    "deal": ["deals"],
    "company": ["companies"],
    "client": ["writeback", "companies"],
    "contact": ["contacts"],
    "revenue_period": ["revenue"],
}

# Every targeted retry still pulls the current HubSpot projection (aquira_id
# narrows only the Aquira side); without a cap, a 50-row backlog turns one
# pass into 50 full-tenant syncs and starves the scheduled poll behind it.
ENQUEUE_CAP_PER_PASS = 8


def run_reconciliation() -> dict[str, Any]:
    settings = get_settings()
    if bool(settings.whatif):
        return {"retried": 0, "frozen": 0, "skipped": "plan-only mode"}

    from app.db.repo import Repo, dlq_budgets

    repo = Repo()
    try:
        try:
            from app.runtime import apply_db_overlay

            # Same Settings singleton, refreshed from the settings table —
            # the admin's stage-mapping fix on the web container only reaches
            # this process through the DB overlay.
            apply_db_overlay()
        except Exception:
            logger.debug("settings overlay refresh failed; using boot-time values", exc_info=True)
        from app.sync.worker import is_busy

        if is_busy():
            return {"retried": 0, "frozen": 0, "skipped": "worker busy"}

        _minutes, budget = dlq_budgets(settings)
        due = repo.due_dead_letters(datetime.utcnow())
        if not due:
            return {"retried": 0, "frozen": 0}
        grouped: dict[tuple[str, str], list[Any]] = {}
        for row in due:
            etype = str(row.entity_type or "")
            aid = str(row.aquira_id or "").strip()
            if etype == "revenue_period" and ":" in aid:
                # The row id is the synthetic period key
                # "<contract>:<month>:<station>"; a targeted sync can only be
                # keyed by the owning contract.
                aid = aid.split(":", 1)[0]
            grouped.setdefault((etype, aid), []).append(row)

        from app.sync.orchestrator import SyncContext
        from app.sync.worker import enqueue_sync

        enqueued = 0
        charged_ids: list[int] = []
        held: dict[str, list[int]] = {}
        for (etype, aid), rows in sorted(grouped.items()):
            if enqueued >= ENQUEUE_CAP_PER_PASS:
                break  # the rest stays due; mark_reconciled never saw them
            ids = [int(r.id) for r in rows]
            if etype == "client" and not aid:
                held.setdefault(
                    "Failed Aquira client create — the create gate holds it for a human "
                    "(a create can commit in Aquira and still report failure; blind retry "
                    "would duplicate the client). Fix the cause, then mark the row Resolved.",
                    [],
                ).extend(ids)
                continue
            entities = ENTITY_TO_SYNC.get(etype, ["deals", "companies", "contacts"])
            if "writeback" in entities and not bool(settings.sync_writeback):
                held.setdefault("sync_writeback is off — this record cannot be written back.", []).extend(ids)
                continue
            if min(int(r.attempts or 0) for r in rows) >= budget:
                # Already past budget when this pass began: a final full sync
                # would be pure waste. Hold it with the reason instead.
                held.setdefault("Retry budget exhausted before this pass — held for review.", []).extend(ids)
                continue
            try:
                enqueue_sync(SyncContext(trigger="reconcile", whatif=False, entities=entities, aquira_id=aid or None))
                enqueued += 1
                charged_ids.extend(ids)
            except Exception:
                # A queue we could not write to is not the record's fault:
                # these rows keep their budget and stay due for the next pass.
                logger.exception("reconciliation could not queue a retry for %s %s", etype, aid)
        updated = repo.mark_reconciled(charged_ids)
        frozen_rows = [row for row in updated if row.status == "frozen"]
        for reason, ids in held.items():
            frozen_rows.extend(repo.freeze_dead_letters(ids, reason))
        if frozen_rows:
            try:
                from app import alerts

                detail = ", ".join(f"{r.entity_type} {r.aquira_id or r.hubspot_id}" for r in frozen_rows[:6])
                more = f" (+{len(frozen_rows) - 6} more)" if len(frozen_rows) > 6 else ""
                alerts.notify_teams(
                    f"HubQuira: {len(frozen_rows)} failed record write(s) stopped retrying and "
                    f"need review on /ui/deadletters: {detail}{more}"
                )
            except Exception:
                logger.debug("frozen-row alert failed", exc_info=True)
        held_count = sum(len(ids) for ids in held.values())
        repo.add_event(
            "reconcile",
            "INFO",
            f"dead-letter reconciliation: {len(due)} due, {enqueued} retries queued"
            + (f", {held_count} held without a cycle" if held_count else "")
            + (f", {len(frozen_rows)} frozen" if frozen_rows else ""),
            {"targets": [f"{e}/{a or 'create'}" for (e, a) in sorted(grouped)][:20]},
        )
        return {"retried": enqueued, "frozen": len(frozen_rows), "held": held_count}
    finally:
        try:
            repo.close()
        except Exception:
            pass


def schedule_reconciliation(scheduler: Any) -> None:
    from app.db.repo import dlq_budgets

    minutes, _budget = dlq_budgets(get_settings())
    scheduler.add_job(
        run_reconciliation,
        "interval",
        minutes=minutes,
        # Timezone-aware on purpose: a naive datetime is read as
        # scheduler-local time (America/Chicago), which puts the first pass
        # ~5 h after boot instead of 2 minutes.
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
        replace_existing=True,
        id="dead_letter_reconcile",
    )
