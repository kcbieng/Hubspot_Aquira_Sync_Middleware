"""SMTP notifications. Currently one consumer: the daily match-review digest.

Email is strictly best-effort — a broken SMTP config logs and degrades to the
in-app queue on /ui/matches, it never fails a sync or a scheduled job."""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

from app.db.repo import Repo
from app.settings import get_settings

logger = logging.getLogger(__name__)

DIGEST_WINDOW = timedelta(hours=20)


def send_email(to: str, subject: str, body: str) -> bool:
    settings = get_settings()
    if not settings.smtp_host or not to:
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from or settings.smtp_user or "hubquira@localhost"
    message["To"] = to
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, int(settings.smtp_port or 587), timeout=15) as smtp:
            if int(settings.smtp_port or 587) == 587:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
        return True
    except Exception as exc:
        logger.warning("match digest email to %s failed: %s", to, exc)
        return False


def run_match_digest(repo_factory: Any = Repo) -> dict[str, Any]:
    settings = get_settings()
    if not settings.match_digest_enabled or not settings.smtp_host:
        return {"status": "skipped", "reason": "email not configured"}
    repo = repo_factory()
    try:
        due = repo.pending_suggestions_due(datetime.utcnow() - DIGEST_WINDOW)
        by_recipient: dict[str, list[Any]] = {}
        admin_emails = [u.email for u in repo.list_users() if u.role == "admin" and u.notify]
        for row in due:
            recipient = (row.assignee_email or (admin_emails[0] if admin_emails else "")).strip()
            if not recipient:
                continue
            by_recipient.setdefault(recipient, []).append(row)
        base = (settings.public_base_url or "").rstrip("/")
        link = f"{base}/ui/matches" if base else "/ui/matches"
        sent = 0
        for recipient, rows in sorted(by_recipient.items()):
            lines = [f"- {r.aquira_name or r.aquira_id} (Aquira {r.aquira_id}) looks like {r.hubspot_name or r.hubspot_id} (HubSpot {r.hubspot_id}) — {r.method or 'match rule'}" for r in rows]
            body = (
                f"{len(rows)} potential duplicate{'s' if len(rows) != 1 else ''} between Aquira and HubSpot "
                f"need a decision. Nothing was changed — open {link}, then Link the real match or "
                f"mark it 'Not a duplicate'.\n\n" + "\n".join(lines) + "\n"
            )
            if send_email(recipient, f"HubQuira: {len(rows)} record match(es) need review", body):
                sent += 1
                repo.mark_suggestions_notified(rows)
        if due:
            repo.add_event(
                "notify",
                "INFO",
                f"match digest: {len(due)} due, emailed {sent} recipient(s)",
                {"recipients": sorted(by_recipient)},
            )
        return {"status": "ok", "due": len(due), "sent": sent}
    finally:
        try:
            repo.close()
        except Exception:
            pass
