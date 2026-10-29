from fastapi.testclient import TestClient

from api.main import app


def test_health_and_operation(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        assert client.get("/health/live").json()["status"] == "alive"
        response = client.post("/v1/operations", json={"command": "plan", "variables": {}})
        assert response.status_code == 200
        operation_id = response.json()["operation_id"]
        detail = client.get(f"/v1/operations/{operation_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "pending"

