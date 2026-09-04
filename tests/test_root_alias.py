from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_redirects_to_ui():
    response = client.get("/", follow_redirects=False)
    assert response.status_code in {302, 303, 307}
    assert response.headers["location"].endswith("/ui")


def test_sync_alias_routes_match_api():
    aliased = client.post("/sync/run", json={"whatif": True, "entities": ["companies"], "wait": True})
    api = client.post("/api/sync/run", json={"whatif": True, "entities": ["companies"], "wait": True})
    assert aliased.status_code == 200
    assert api.status_code == 200
    assert aliased.json()["status"] in {"success", "partial", "error"}
    assert client.get("/sync/status").status_code == 200
