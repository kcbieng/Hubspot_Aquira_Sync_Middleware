from __future__ import annotations

from sqlalchemy import delete, select

from app.db.models import DeadLetter, RevenuePeriod
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

    repo.session.execute(delete(RevenuePeriod))
    repo.session.commit()
    repo.add_revenue_period({
        "aquira_id": "9001:2026-01:10",
        "contract_id": 9001,
        "period": "2026-01-01",
        "amount": 4000.0,
        "station": "KCBI",
        "station_id": 10,
        "kind": "booked",
        "contract_cd": "C-9001",
    })
    repo.add_revenue_period({
        "aquira_id": "9001:2026-02:10",
        "contract_id": 9001,
        "period": "2026-02-01",
        "amount": 4000.0,
        "station": "KCBI",
        "station_id": 10,
        "kind": "booked",
        "contract_cd": "C-9001",
    })

    repo.delete_stale_revenue_periods(9001, {"9001:2026-02:10"})
    remaining = repo.session.execute(select(RevenuePeriod).where(RevenuePeriod.contract_id == 9001)).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].aquira_id == "9001:2026-02:10"
