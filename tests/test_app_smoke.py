from fastapi.testclient import TestClient

from app.main import app


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
