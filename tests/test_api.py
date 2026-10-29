from fastapi.testclient import TestClient

from api.main import app


def test_health_and_operation(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        assert client.get("/health/live").json()["status"] == "alive"
        response = client.post(
            "/v1/operations",
            json={
                "command": "plan",
                "action": "provision",
                "stack": "local",
                "spec": {"name": "local-test", "image": "ubuntu-2404"},
            },
        )
        assert response.status_code == 200
        operation_id = response.json()["operation_id"]
        detail = client.get(f"/v1/operations/{operation_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "pending"


def test_swagger_exposes_bearer_and_protects_operations(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_API_TOKEN", "swagger-test-token")
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert "Bearer" in schema["components"]["securitySchemes"]
        assert "/v1/operations" in schema["paths"]
        assert client.get("/v1/plans").status_code == 401
        response = client.get(
            "/v1/plans", headers={"Authorization": "Bearer swagger-test-token"}
        )
        assert response.status_code == 200

