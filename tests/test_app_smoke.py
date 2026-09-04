from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings
from app.sync.worker import wait_for_run


client = TestClient(app)


def test_app_health_and_ready_endpoints():
    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["service"] == "HubQuira"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_ui_login_accepts_default_credentials():
    login_page = client.get("/ui/login")
    assert login_page.status_code == 200

    response = client.post(
        "/ui/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui"


def test_dashboard_has_live_runtime_controls():
    client.post(
        "/ui/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )

    dashboard = client.get("/ui")
    assert dashboard.status_code == 200
    html = dashboard.text
    assert "HubQuira" in html
    assert 'name="whatif"' in html
    assert 'name="sync_interval_minutes"' in html
    assert 'Run sync' in html


def test_dashboard_settings_form_updates_runtime_state():
    client.post(
        "/ui/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )

    response = client.post(
        "/ui/settings",
        data={"whatif": "false", "sync_interval_minutes": "45"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui"
    settings = get_settings()
    assert settings.whatif is False
    assert settings.sync_interval_minutes == 45


def test_dashboard_shows_recent_sync_output():
    client.post(
        "/ui/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )

    client.post("/ui/sync/run", follow_redirects=False)
    # worker finishes in the background; wait for something to land
    from app.db.repo import Repo

    latest = Repo().latest_run()
    if latest:
        wait_for_run(latest.id, timeout=15)
    dashboard = client.get("/ui")
    html = dashboard.text

    assert "Last sync output" in html
    assert "companies" in html.lower()
    assert "planned" in html.lower()


def test_ui_whatif_redirects_to_persisted_run():
    client.post(
        "/ui/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )
    response = client.post("/ui/sync/run", data={"whatif": "true"}, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers.get("location") or ""
    assert location.startswith("/ui/runs/")
    run_id = int(location.rsplit("/", 1)[-1])
    wait_for_run(run_id, timeout=15)
    detail = client.get(location)
    assert detail.status_code == 200
    assert "Run not found" not in detail.text
    assert "Run #" in detail.text


def test_api_sync_run_records_status_and_history():
    run_response = client.post(
        "/api/sync/run",
        json={"whatif": True, "entities": ["clients", "contracts"], "wait": True},
    )

    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["status"] in {"success", "partial", "error", "queued"}
    if payload.get("run_id") and payload["status"] == "queued":
        payload = wait_for_run(int(payload["run_id"]), timeout=15) or payload
    assert payload["status"] in {"success", "partial", "error"}
    assert payload.get("whatif") in {True, "true", None}

    status_response = client.get("/api/sync/status")
    assert status_response.status_code == 200
    assert status_response.json()["whatif"] in {True, "true"}

    history_response = client.get("/api/sync/runs")
    assert history_response.status_code == 200
    assert len(history_response.json()) >= 1


def test_required_operator_pages_and_api_surfaces_exist():
    login = client.post(
        "/ui/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    settings_page = client.get("/ui/settings")
    owners_page = client.get("/ui/owners")
    teams_page = client.get("/ui/teams")
    runs_page = client.get("/ui/runs")
    logs_page = client.get("/ui/logs")

    assert settings_page.status_code == 200
    assert owners_page.status_code == 200
    assert teams_page.status_code == 200
    assert runs_page.status_code == 200
    assert logs_page.status_code == 200

    suggest_response = client.get("/api/owners/suggest")
    assert suggest_response.status_code == 200
    assert isinstance(suggest_response.json(), list)

    team_suggest = client.get("/api/teams/suggest")
    assert team_suggest.status_code == 200
    assert isinstance(team_suggest.json(), list)

    run_detail_response = client.get("/api/sync/runs/1")
    assert run_detail_response.status_code in {200, 404}
