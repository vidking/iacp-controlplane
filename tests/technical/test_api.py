import json
import os
import uuid

from fastapi.testclient import TestClient

from api.db import database
from api.main import app


def auth():
    return {"Authorization": "Bearer technical-test-token"}


def test_api_contract_has_only_technical_resources():
    schema = app.openapi()
    assert "/v1/instances" in schema["paths"]
    assert "/v1/operations" in schema["paths"]
    assert all("commercial" not in path.lower() for path in schema["paths"])
    assert all("tenant" not in json.dumps(value).lower() for value in schema["paths"].values())


def test_catalog_publishes_all_established_plans_and_images():
    with TestClient(app) as client:
        plans = client.get("/v1/plans", headers=auth())
        images = client.get("/v1/images", headers=auth())
    assert plans.status_code == 200
    assert plans.json()["plans"] == [
        {"plan_id": "kvm-1", "name": "KVM 1", "vcpus": 1, "memory_mb": 2048, "disk_gb": 50},
        {"plan_id": "kvm-2", "name": "KVM 2", "vcpus": 2, "memory_mb": 4096, "disk_gb": 100},
        {"plan_id": "kvm-3", "name": "KVM 3", "vcpus": 4, "memory_mb": 8192, "disk_gb": 200},
        {"plan_id": "kvm-4", "name": "KVM 4", "vcpus": 8, "memory_mb": 16384, "disk_gb": 400},
    ]
    assert images.status_code == 200
    assert [item["image_id"] for item in images.json()["images"]] == [
        "ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10"
    ]


def test_mng_plan_is_resolved_from_catalog_before_persistence():
    name = f"technical-plan-{uuid.uuid4().hex[:10]}"
    with TestClient(app) as client:
        response = client.post(
            "/v1/instances",
            headers=auth(),
            json={
                "stack": "mng01",
                "spec": {
                    "name": name,
                    "image": "ubuntu-2404",
                    "plan_id": "kvm-1",
                    "root_password": "Temporary-Root-123!",
                    "ssh_public_keys": [],
                },
            },
        )
    assert response.status_code == 200
    with database() as db:
        row = db.execute(
            "SELECT spec_json FROM instances WHERE instance_id=?",
            (response.json()["instance_id"],),
        ).fetchone()
    spec = json.loads(row["spec_json"])
    assert spec["plan_id"] == "kvm-1"
    assert spec["vcpus"] == 1
    assert spec["memory_mb"] == 2048
    assert spec["disk_gb"] == 50


def test_mng_rejects_resource_override_that_disagrees_with_plan():
    with TestClient(app) as client:
        response = client.post(
            "/v1/instances",
            headers=auth(),
            json={
                "stack": "mng01",
                "spec": {
                    "name": f"technical-mismatch-{uuid.uuid4().hex[:10]}",
                    "image": "ubuntu-2404",
                    "plan_id": "kvm-1",
                    "vcpus": 2,
                    "root_password": "Temporary-Root-123!",
                    "ssh_public_keys": [],
                },
            },
        )
    assert response.status_code == 422
    assert "vcpus" in response.text


def test_root_password_is_encrypted_and_never_persisted_plaintext():
    password = "Temporary-Root-123!"
    name = f"technical-secret-{uuid.uuid4().hex[:10]}"
    with TestClient(app) as client:
        response = client.post(
            "/v1/instances",
            headers=auth(),
            json={
                "stack": "local",
                "spec": {
                    "name": name,
                    "image": "ubuntu-2404",
                    "root_password": password,
                    "ssh_public_keys": [],
                },
            },
        )
    assert response.status_code == 200
    operation_id = response.json()["operation_id"]
    with TestClient(app) as client:
        public_operation = client.get(f"/v1/operations/{operation_id}", headers=auth())
    assert "access_ciphertext" not in public_operation.json()
    assert password not in public_operation.text
    with database() as db:
        operation = db.execute(
            "SELECT payload_json, access_ciphertext FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        instance = db.execute(
            "SELECT spec_json FROM instances WHERE instance_id=?",
            (response.json()["instance_id"],),
        ).fetchone()
    assert password not in operation["payload_json"]
    assert password not in instance["spec_json"]
    assert operation["access_ciphertext"]
    assert password not in operation["access_ciphertext"]


def test_mng_requires_root_password_for_real_access_verification():
    with TestClient(app) as client:
        response = client.post(
            "/v1/instances",
            headers=auth(),
            json={
                "stack": "mng01",
                "spec": {
                    "name": f"technical-access-{uuid.uuid4().hex[:10]}",
                    "image": "ubuntu-2404",
                    "plan_id": "kvm-1",
                    "ssh_public_keys": ["ssh-ed25519 AAAATEST technical"],
                },
            },
        )
    assert response.status_code == 422
    assert "root_password" in response.text


def test_protected_api_fails_closed_when_token_configuration_is_missing(monkeypatch):
    monkeypatch.delenv("IACP_API_TOKEN", raising=False)
    monkeypatch.delenv("IACP_API_TOKEN_FILE", raising=False)
    with TestClient(app) as client:
        response = client.get("/v1/plans")
    assert response.status_code == 503


def test_readiness_requires_alembic_revision():
    from api.db import schema_revision

    assert schema_revision() is not None
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
