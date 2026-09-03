from __future__ import annotations

from sqlalchemy import delete, select

from app.db.models import DeadLetter
from app.db.repo import Repo


def test_repo_tracks_cursor_and_dead_letter():
    repo = Repo()
    repo.session.execute(delete(DeadLetter))
    repo.session.commit()

    repo.set_cursor(
        "clients",
        last_started="2026-01-01T00:00:00",
        last_finished="2026-01-01T00:05:00",
        last_error=None,
        last_success_at="2026-01-01T00:05:00",
    )
    cursor = repo.get_cursor("clients")

    assert cursor is not None
    assert cursor.job == "clients"
    assert cursor.last_success_at is not None

    repo.add_dead_letter("client", "42", "Boom", payload={"name": "Acme"}, attempts=1)
    dead_letter = repo.session.execute(select(DeadLetter).where(DeadLetter.aquira_id == "42")).scalar_one_or_none()
    assert dead_letter is not None
    assert dead_letter.entity_type == "client"
    assert dead_letter.error == "Boom"
