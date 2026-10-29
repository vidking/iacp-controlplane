from fastapi.testclient import TestClient

from api.db import database, initialize
from api.main import app
from worker.proxmox import ProxmoxError


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


def test_instances_endpoints_create_list_and_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            json={
                "stack": "local",
                "spec": {"name": "console-local", "image": "ubuntu-2404", "started": False},
            },
        )
        assert created.status_code == 200
        instance_id = created.json()["instance_id"]

        listing = client.get("/v1/instances")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["instances"][0]["instance_id"] == instance_id

        detail = client.get(f"/v1/instances/{instance_id}")
        assert detail.status_code == 200
        assert detail.json()["stack"] == "local"
        assert detail.json()["spec"]["name"] == "console-local"


def test_power_routes_are_listed_and_queue_without_power_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            json={"stack": "mng01", "spec": {"name": "power-contract", "started": False}},
        )
        instance_id = created.json()["instance_id"]
        plan_id = created.json()["operation_id"]
        initialize()
        with database() as db:
            db.execute(
                "UPDATE operations SET status='succeeded' WHERE operation_id=?", (plan_id,)
            )
            db.execute(
                "UPDATE instances SET status='ready', vmid=40000 WHERE instance_id=?", (instance_id,)
            )

        response = client.post(f"/v1/instances/{instance_id}/power/shutdown")
        assert response.status_code == 202
        operation_id = response.json()["operation_id"]
        operation = client.get(f"/v1/operations/{operation_id}").json()
        assert operation["command"] == "power"
        assert operation["payload"]["power_action"] == "shutdown"

        schema = client.get("/openapi.json").json()
        assert "/v1/instances" in schema["paths"]
        assert "/v1/instances/{instance_id}/power/start" in schema["paths"]
        assert "/v1/instances/{instance_id}/power/shutdown" in schema["paths"]
        assert "/v1/instances/{instance_id}/power/reboot" in schema["paths"]


def test_power_state_reports_missing_permission_without_hiding_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            json={"stack": "mng01", "spec": {"name": "power-read", "started": False}},
        )
        instance_id = created.json()["instance_id"]
        with database() as db:
            db.execute(
                "UPDATE instances SET status='ready', vmid=40001 WHERE instance_id=?", (instance_id,)
            )

        class FakeProxmox:
            def permissions(self, node, vmid, *, mutate=False):
                if mutate:
                    raise ProxmoxError("POWER_PERMISSION_DENIED", 403)

            def power_state(self, node, vmid, name):
                return {"power_state": "stopped", "observed_at": "now", "qmp_status": "stopped", "uptime": 0}

        monkeypatch.setattr("api.main.ProxmoxClient", FakeProxmox)
        response = client.get(f"/v1/instances/{instance_id}/power")
        assert response.status_code == 200
        assert response.json()["power_state"] == "stopped"
        assert response.json()["mutations_available"] is False
        assert response.json()["blocked_reason"] == "POWER_PERMISSION_DENIED"


def test_console_is_available_without_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        response = client.get("/console")
        assert response.status_code == 200
        assert "Crear y administrar" in response.text

