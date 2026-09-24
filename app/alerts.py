"""Teams channel alerts for the failure modes that currently only live in logs:
a run that errored or partially failed, and a certified-pull expectation that
wasn't met (revenue pruning suppressed). Alerting is strictly best-effort —
an unreachable webhook must never change a run's outcome."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.settings import get_settings

logger = logging.getLogger(__name__)


def notify_teams(text: str) -> bool:
    url = (get_settings().teams_webhook_url or "").strip()
    if not url or not text:
        return False
    # M365 Workflows "HTTP Request" trigger payload; the legacy O365 connector
    # form ({"text": ...}) is still accepted by older webhook URLs too.
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/adaptivecard",
                "content": {
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [{"type": "TextBlock", "wrap": True, "text": text}],
                },
            }
        ],
        "text": text,
    }
    try:
        response = httpx.post(url, json=payload, timeout=10.0)
        return response.status_code < 400
    except Exception as exc:
        logger.warning("Teams alert failed: %s", exc)
        return False


def report_run(
    *,
    status: str,
    error_count: int = 0,
    notices: list[str] | None = None,
    warnings: list[str] | None = None,
    run_id: Any = None,
    whatif: bool = False,
    exception: str | None = None,
) -> bool:
    """One line per alert-worthy outcome; a clean run says nothing."""
    notices = notices or []
    warnings = warnings or []
    if status == "success" and not notices and not warnings:
        return False
    label = {"error": "FAILED", "partial": "PARTIAL", "success": "clean but noisy"}
    lines = [
        f"HubQuira sync #{run_id or '?'} {label.get(status, status.upper())}"
        + (" (what-if)" if whatif else ""),
    ]
    if exception:
        lines.append(exception[:400])
    if error_count:
        lines.append(f"{error_count} item error(s) — dead letters may need review on /ui/deadletters")
    for notice in notices[:3]:
        lines.append(notice[:300])
    for warning in warnings[:3]:
        lines.append(warning[:300])
    return notify_teams("\n".join(lines))
