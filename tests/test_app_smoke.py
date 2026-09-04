from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings


client = TestClient(app)


def test_app_health_and_ready_endpoints():
    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
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
    dashboard = client.get("/ui")
    html = dashboard.text

    assert "Last sync output" in html
    assert "companies" in html.lower()
    lower_html = html.lower()
    assert "planned" in lower_html or "applied" in lower_html
    assert "what-if" in lower_html or "live" in lower_html


def test_api_sync_run_records_status_and_history():
    run_response = client.post(
        "/api/sync/run",
        json={"whatif": True, "entities": ["clients", "contracts"]},
    )

    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["status"] == "success"
    assert payload["whatif"] is True

    status_response = client.get("/api/sync/status")
    assert status_response.status_code == 200
    assert status_response.json()["whatif"] in {True, "true"}

    history_response = client.get("/api/sync/runs")
    assert history_response.status_code == 200
    assert len(history_response.json()) >= 1


def test_settings_validation_endpoints_accept_browser_gets():
    aquira_response = client.get("/api/settings/test/aquira")
    hubspot_response = client.get("/api/settings/test/hubspot")

    assert aquira_response.status_code == 200
    assert hubspot_response.status_code == 200
    assert aquira_response.json()["status"] in {"ok", "error"}
    assert hubspot_response.json()["status"] in {"ok", "error"}


def test_required_operator_pages_and_api_surfaces_exist():
    login = client.post(
        "/ui/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    settings_page = client.get("/ui/settings")
    owners_page = client.get("/ui/owners")
    runs_page = client.get("/ui/runs")
    logs_page = client.get("/ui/logs")

    assert settings_page.status_code == 200
    assert owners_page.status_code == 200
    assert runs_page.status_code == 200
    assert logs_page.status_code == 200

    suggest_response = client.get("/api/owners/suggest")
    assert suggest_response.status_code == 200
    assert isinstance(suggest_response.json(), list)

    run_detail_response = client.get("/api/sync/runs/1")
    assert run_detail_response.status_code in {200, 404}
