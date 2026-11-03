from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.db import database, initialize
from api.main import app
from worker.proxmox import ProxmoxError


PUBLIC_KEY = "ssh-ed25519 AAAAexample"


def public_body(name: str = "console-local", *, password_only: bool = False) -> dict:
    access = {"ssh_public_keys": [PUBLIC_KEY]}
    if password_only:
        access = {"root_password": "Inicial-Segura-123!", "ssh_public_keys": []}
    return {
        "name": name,
        "plan_id": "kvm-2",
        "image_id": "ubuntu-2404",
        "access": access,
        "commercial_context": {
            "customer_id": "CL-0000",
            "customer_tag": "cl-0000",
            "order_id": "ORD-1001",
            "product_id": "KVM-2",
        },
        "start_on_create": False,
    }


def test_health_and_internal_operation(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        assert client.get("/health/live").json()["status"] == "alive"
        response = client.post(
            "/v1/operations",
            json={
                "command": "plan",
                "action": "provision",
                "stack": "local",
                "plan_id": "kvm-1",
                "spec": {"name": "local-test", "image": "ubuntu-2404"},
            },
        )
        assert response.status_code == 200
        operation_id = response.json()["operation_id"]
        detail = client.get(f"/v1/operations/{operation_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "queued"
        assert set(detail.json()["timings"]) == {"queue_seconds", "execution_seconds", "total_seconds"}


def test_health_response_reports_the_running_build_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    revision = "a" * 40
    monkeypatch.setenv("IACP_BUILD_REVISION", revision)
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.headers["X-IACP-Build-Revision"] == revision


def test_catalog_exposes_stable_plan_and_golden_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        plans = client.get("/v1/plans")
        assert plans.status_code == 200
        assert plans.json()["plans"] == [
            {"plan_id": "kvm-1", "vcpus": 1, "memory_mb": 2048, "disk_gb": 50},
            {"plan_id": "kvm-2", "vcpus": 2, "memory_mb": 4096, "disk_gb": 100},
            {"plan_id": "kvm-3", "vcpus": 4, "memory_mb": 8192, "disk_gb": 200},
            {"plan_id": "kvm-4", "vcpus": 8, "memory_mb": 16384, "disk_gb": 400},
        ]
        images = client.get("/v1/images").json()["images"]
        assert [(item["image_id"], item["template_vm_id"]) for item in images] == [
            ("ubuntu-2404", 29000),
            ("ubuntu-2604", 29001),
            ("almalinux-9", 29010),
            ("almalinux-10", 29011),
        ]


def test_network_profiles_keep_dhcp_default_and_hide_unconfigured_static_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.delenv("IACP_PROD_STATIC_ENABLED", raising=False)
    monkeypatch.delenv("IACP_NETBOX_URL", raising=False)
    monkeypatch.delenv("IACP_NETBOX_PREFIX_ID", raising=False)
    with TestClient(app) as client:
        response = client.get("/v1/network-profiles")
        assert response.status_code == 200
        profiles = response.json()["network_profiles"]
        assert profiles[0]["profile_id"] == "lab_dhcp"
        assert profiles[0]["enabled"] is True
        assert profiles[1]["profile_id"] == "prod_static"
        assert profiles[1]["enabled"] is False


def test_prod_static_requires_explicitly_enabled_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.delenv("IACP_PROD_STATIC_ENABLED", raising=False)
    body = public_body("static-disabled")
    body["network_profile"] = "prod_static"
    with TestClient(app) as client:
        response = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "static-disabled"},
            json=body,
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "NETWORK_PROFILE_NOT_CONFIGURED"


def test_prod_static_request_persists_profile_without_accepting_an_ip_from_consumer(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_PROD_STATIC_ENABLED", "1")
    monkeypatch.setenv("IACP_NETBOX_URL", "https://netbox.cyberfuel.com/api")
    monkeypatch.setenv("IACP_NETBOX_PREFIX_ID", "123")
    body = public_body("static-profile")
    body["network_profile"] = "prod_static"
    with TestClient(app) as client:
        response = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "static-profile"},
            json=body,
        )
        assert response.status_code == 202
        detail = client.get(f"/v1/instances/{response.json()['instance_id']}")
        assert detail.status_code == 200
        value = detail.json()
        assert value["spec"]["network_profile"] == "prod_static"
        assert value["spec"]["network_mode"] == "static"
        assert value["spec"]["ipv4_cidr"] is None
        assert value["network_allocation"]["status"] == "pending"


def test_swagger_exposes_bearer_and_protects_operations(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_API_TOKEN", "swagger-test-token")
    with TestClient(app) as client:
        portal = client.get("/docs")
        assert portal.status_code == 200
        assert "Crear y administrar máquinas virtuales" in portal.text
        assert "JSON de ejemplo" in portal.text
        assert 'id="json-preview"' in portal.text
        assert 'id="create-instances-table"' in portal.text
        assert 'id="instance-status-filter"' in portal.text
        assert 'id="instance-page-size"' in portal.text
        assert 'id="operation-page-size"' in portal.text
        assert 'id="worker-operation-detail"' in portal.text
        assert 'id="operation-timeline"' in portal.text
        assert 'id="worker-operation-timeline"' in portal.text
        assert 'id="network-profile"' in portal.text
        assert 'id="network-profile-help"' in portal.text
        assert 'id="vm-tags"' in portal.text
        assert "customer_tag" in portal.text
        assert 'id="obs-live-status"' in portal.text
        assert 'id="workers-live-status"' in portal.text
        assert "Cómo consumir la API" in portal.text
        assert "Solo listas (ready)" in portal.text
        assert "Apagadas (stopped)" in portal.text
        assert 'id="global-alert"' not in portal.text
        assert client.get("/docs/assets/docs.css").status_code == 200
        assert client.get("/docs/assets/docs.js").status_code == 200
        swagger = client.get("/swagger")
        assert swagger.status_code == 200
        assert "SwaggerUIBundle" in swagger.text
        redoc = client.get("/redoc")
        assert redoc.status_code == 200
        schema = client.get("/openapi.json").json()
        assert "/v1/network-profiles" in schema["paths"]
        assert "redoc" in redoc.text.lower()
        schema = client.get("/openapi.json").json()
        assert "Bearer" in schema["components"]["securitySchemes"]
        assert "/v1/operations" in schema["paths"]
        assert "/v1/instances" in schema["paths"]
        request_schema = schema["components"]["schemas"]["InstanceCreateRequest"]
        assert request_schema["additionalProperties"] is False
        assert "tags" in request_schema["properties"]
        assert "etiquetas proxmox adicionales" in request_schema["properties"]["tags"]["description"].lower()
        assert request_schema["properties"]["tags"]["items"]["maxLength"] == 63
        assert request_schema["properties"]["tags"]["items"]["pattern"] == r"^[a-z0-9][a-z0-9_-]{0,62}$"
        assert schema["paths"]["/v1/instances"]["get"]["operationId"] == "listInstances"
        assert schema["paths"]["/v1/instances/{instance_id}/power/start"]["post"]["tags"] == ["kvm-lifecycle"]
        assert "/v1/instances/{instance_id}/network" in schema["paths"]
        assert client.get("/v1/plans").status_code == 401
        response = client.get(
            "/v1/plans", headers={"Authorization": "Bearer swagger-test-token"}
        )
        assert response.status_code == 200


def test_network_observation_uses_guest_agent_and_does_not_guess_ip(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-network-observation"},
            json=public_body("network-observation"),
        )
        assert created.status_code == 202
        instance_id = created.json()["instance_id"]
        with database() as db:
            db.execute(
                "UPDATE instances SET status='ready', vmid=40100, node='mng02' WHERE instance_id=?",
                (instance_id,),
            )

        class FakeProxmox:
            def __init__(self):
                self.calls = []

            def guest_agent_interfaces(self, node, vmid):
                self.calls.append((node, vmid))
                return [
                    {
                        "name": "lo",
                        "ip-addresses": [
                            {"ip-address": "127.0.0.1", "ip-address-type": "ipv4"},
                        ],
                    },
                    {
                        "name": "eth0",
                        "ip-addresses": [
                            {"ip-address": "10.20.30.40", "ip-address-type": "ipv4"},
                            {"ip-address": "10.20.30.40", "ip-address-type": "ipv4"},
                            {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
                        ],
                    },
                ]

        fake = FakeProxmox()
        monkeypatch.setattr("api.main.ProxmoxClient", lambda: fake)
        response = client.get(f"/v1/instances/{instance_id}/network")
        assert response.status_code == 200
        assert response.json()["observed_ipv4"] == ["10.20.30.40"]
        assert response.json()["source"] == "guest_agent"
        assert response.json()["status"] == "observed"
        assert fake.calls == [("mng02", 40100)]


def test_network_observation_reports_pending_before_provisioning(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-network-pending"},
            json=public_body("network-pending"),
        )
        instance_id = created.json()["instance_id"]
        response = client.get(f"/v1/instances/{instance_id}/network")
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert response.json()["reason"] == "VM_NOT_PROVISIONED"


def test_instances_endpoint_is_one_step_and_preserves_context(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    body = public_body(password_only=True)
    body["tags"] = ["kvm-001", "postal", "kvm-001", "cl-0000"]
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-console-local"},
            json=body,
        )
        assert created.status_code == 202
        response = created.json()
        assert response["status"] == "queued"
        assert response["job_id"] == response["operation_id"]

        operation = client.get(f"/v1/operations/{response['operation_id']}")
        assert operation.status_code == 200
        assert operation.json()["commercial_context"]["customer_id"] == "CL-0000"
        assert "Inicial-Segura-123!" not in operation.text

        listing = client.get("/v1/instances")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["instances"][0]["instance_id"] == response["instance_id"]

        detail = client.get(f"/v1/instances/{response['instance_id']}")
        assert detail.status_code == 200
        value = detail.json()
        assert value["cluster"] == "mng"
        assert value["stack"] == "mng01"
        assert value["plan_id"] == "kvm-2"
        assert value["image_id"] == "ubuntu-2404"
        assert value["spec"]["vcpus"] == 2
        assert value["spec"]["memory_mb"] == 4096
        assert value["spec"]["disk_gb"] == 100
        assert value["spec"]["tags"] == ["cl-0000", "kvm-001", "postal"]
        assert value["commercial_context"]["customer_tag"] == "cl-0000"


def test_public_request_validates_additional_tags_and_rejects_unknown_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    invalid_bodies = []
    for tag in ("KVM-001", "-invalid", "x" * 64):
        body = public_body(f"tag-invalid-{len(invalid_bodies)}")
        body["tags"] = [tag]
        invalid_bodies.append(body)
    unknown_field_body = public_body("unknown-environment")
    unknown_field_body["environment"] = "lab"
    invalid_bodies.append(unknown_field_body)

    with TestClient(app) as client:
        for index, body in enumerate(invalid_bodies):
            response = client.post(
                "/v1/instances",
                headers={"Idempotency-Key": f"invalid-public-contract-{index}"},
                json=body,
            )
            assert response.status_code == 422


def test_ready_and_stopped_are_separate_public_instance_states(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-ready-stopped"},
            json=public_body("ready-stopped"),
        )
        assert created.status_code == 202
        instance_id = created.json()["instance_id"]
        operation_id = created.json()["operation_id"]

        with database() as db:
            db.execute("UPDATE operations SET status='succeeded' WHERE operation_id=?", (operation_id,))
            db.execute(
                "UPDATE instances SET status='ready', vmid=40150, power_state='running' WHERE instance_id=?",
                (instance_id,),
            )

        ready = client.get("/v1/instances?status=ready")
        assert ready.status_code == 200
        assert ready.json()["total"] == 1
        assert ready.json()["instances"][0]["status"] == "ready"

        with database() as db:
            db.execute(
                "UPDATE instances SET power_state='stopped' WHERE instance_id=?",
                (instance_id,),
            )

        assert client.get("/v1/instances?status=ready").json()["total"] == 0
        stopped = client.get("/v1/instances?status=stopped")
        assert stopped.status_code == 200
        assert stopped.json()["total"] == 1
        assert stopped.json()["instances"][0]["status"] == "stopped"
        assert client.get(f"/v1/instances/{instance_id}").json()["status"] == "stopped"

        observability = client.get("/v1/observability")
        assert observability.json()["instance_status_counts"]["stopped"] == 1
        assert observability.json()["instance_status_counts"].get("ready", 0) == 0


def test_events_are_newest_first_enriched_and_filterable_by_operation(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_MAX_CONCURRENT", "6")
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-event-observability"},
            json=public_body("event-observability"),
        )
        assert created.status_code == 202
        instance_id = created.json()["instance_id"]
        operation_id = created.json()["operation_id"]

        with database() as db:
            db.execute(
                "UPDATE operations SET worker_id='worker-test' WHERE operation_id=?",
                (operation_id,),
            )
            db.execute(
                "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, ?, ?, ?, ?)",
                (operation_id, "test.old", "running", "2026-01-01T00:00:00+00:00", "etapa anterior"),
            )
            db.execute(
                "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, ?, ?, ?, ?)",
                (operation_id, "test.new", "completed", "2026-01-01T00:00:01+00:00", "etapa actual"),
            )
            db.execute(
                "INSERT INTO workers(worker_id, status, last_heartbeat, current_operation_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(worker_id) DO UPDATE SET status=EXCLUDED.status, "
                "last_heartbeat=EXCLUDED.last_heartbeat, current_operation_id=EXCLUDED.current_operation_id",
                ("worker-test", "running", "2026-01-01T00:00:01+00:00", operation_id),
            )

        newest = client.get("/v1/events?sort=desc&limit=20")
        assert newest.status_code == 200
        newest_events = newest.json()["events"]
        assert newest_events[0]["kind"] == "test.new"
        assert newest_events[0]["command"] == "provision"
        assert newest_events[0]["action"] == "provision"
        assert newest_events[0]["instance_id"] == instance_id
        assert newest_events[0]["instance_name"] == "event-observability"
        assert newest_events[0]["worker_id"] == "worker-test"

        timeline = client.get(
            f"/v1/events?operation_id={operation_id}&sort=asc&limit=20"
        )
        assert timeline.status_code == 200
        timeline_events = timeline.json()["events"]
        assert timeline_events[-1]["kind"] == "test.new"
        assert timeline_events[0]["operation_id"] == operation_id

        detail = client.get(f"/v1/operations/{operation_id}").json()
        assert detail["latest_event"]["kind"] == "test.new"

        workers = client.get("/v1/workers").json()["workers"]
        worker = next(item for item in workers if item["worker_id"] == "worker-test")
        assert worker["current_operation"]["latest_event"]["kind"] == "test.new"

        observed = client.get("/v1/observability").json()
        assert observed["recent_events"][0]["kind"] == "test.new"
        assert observed["max_concurrent"] == 6
        assert "performance" in observed


def test_observability_reports_recent_operation_timing_percentiles(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        with database() as db:
            db.executemany(
                """
                INSERT INTO operations(
                    operation_id, command, status, payload_json, created_at,
                    started_at, finished_at
                ) VALUES (?, ?, 'succeeded', '{}', ?, ?, ?)
                """,
                [
                    (
                        "timing-provision",
                        "provision",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:01+00:00",
                        "2026-01-01T00:00:11+00:00",
                    ),
                    (
                        "timing-power",
                        "power",
                        "2026-01-01T00:01:00+00:00",
                        "2026-01-01T00:01:02+00:00",
                        "2026-01-01T00:01:22+00:00",
                    ),
                ],
            )
        observed = client.get("/v1/observability").json()
        performance = observed["performance"]
        assert performance["sample_size"] == 2
        assert performance["total"]["p50_seconds"] == 16.5
        assert performance["total"]["p95_seconds"] == 21.45
        assert performance["by_command"]["provision"]["total"]["p50_seconds"] == 11.0
        assert performance["throughput_operations_per_minute"] is not None


def test_public_operation_views_hide_internal_tool_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-public-log-contract"},
            json=public_body("public-log-contract"),
        )
        assert created.status_code == 202
        instance_id = created.json()["instance_id"]
        operation_id = created.json()["operation_id"]
        with database() as db:
            db.execute(
                "UPDATE operations SET status='failed', worker_id='worker-public', output=?, error=? WHERE operation_id=?",
                (
                    "Terraform initializing modules FastAPI bpg/proxmox module.kvm /var/lib/iacp-controlplane",
                    "Terraform apply failed in provider plugin; see HashiCorp output",
                    operation_id,
                ),
            )
            db.execute(
                "UPDATE instances SET status='failed' WHERE instance_id=?",
                (instance_id,),
            )
            db.execute(
                "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, ?, ?, ?, ?)",
                (
                    operation_id,
                    "terraform.apply.failed",
                    "failed",
                    "2026-01-01T00:00:01+00:00",
                    "Terraform provider plugin failed in module.kvm",
                ),
            )
            db.execute(
                "INSERT INTO workers(worker_id, status, last_heartbeat, current_operation_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(worker_id) DO UPDATE SET status=EXCLUDED.status, "
                "last_heartbeat=EXCLUDED.last_heartbeat, current_operation_id=EXCLUDED.current_operation_id",
                ("worker-public", "idle", "2026-01-01T00:00:02+00:00", operation_id),
            )

        responses = [
            client.get(f"/v1/operations/{operation_id}"),
            client.get(f"/v1/instances/{instance_id}"),
            client.get(f"/v1/events?operation_id={operation_id}"),
            client.get("/v1/workers"),
            client.get("/v1/observability"),
        ]
        assert all(response.status_code == 200 for response in responses)
        combined = "\n".join(response.text.lower() for response in responses)
        for marker in ("terraform", "fastapi", "uvicorn", "bpg/proxmox", "hashicorp", "module.kvm", "/var/lib/iacp-controlplane"):
            assert marker not in combined
        operation = responses[0].json()
        assert operation["status"] == "failed"
        assert operation["error_code"] == "OPERATION_FAILED"
        assert operation["latest_event"]["kind"] == "instance.create.failed"
        assert "provider" not in operation["output"].lower()


def test_public_mutations_generate_idempotency_and_replay_safely(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    body = public_body()
    body["tags"] = ["kvm-001"]
    with TestClient(app) as client:
        generated = client.post("/v1/instances", json=public_body("generated-key"))
        assert generated.status_code == 202
        assert generated.json()["idempotency_key"].startswith("generated-")

        first = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "same-request"},
            json=body,
        )
        replay = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "same-request"},
            json=body,
        )
        assert first.status_code == replay.status_code == 202
        assert replay.json() == first.json()

        changed = public_body()
        changed["tags"] = ["kvm-002"]
        conflict = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "same-request"},
            json=changed,
        )
        assert conflict.status_code == 409


def test_network_profile_is_part_of_public_idempotency_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_PROD_STATIC_ENABLED", "1")
    monkeypatch.setenv("IACP_NETBOX_URL", "https://netbox.example.invalid/api")
    monkeypatch.setenv("IACP_NETBOX_PREFIX_ID", "123")
    body = public_body("idempotency-network-profile")

    with TestClient(app) as client:
        first = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "same-network-request"},
            json=body,
        )
        assert first.status_code == 202

        changed = {**body, "network_profile": "prod_static"}
        response = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "same-network-request"},
            json=changed,
        )
        assert response.status_code == 409


def test_power_routes_are_listed_and_queue_with_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-power-contract"},
            json=public_body("power-contract"),
        )
        instance_id = created.json()["instance_id"]
        plan_id = created.json()["operation_id"]
        with database() as db:
            db.execute("UPDATE operations SET status='succeeded' WHERE operation_id=?", (plan_id,))
            db.execute(
                "UPDATE instances SET status='ready', vmid=40000 WHERE instance_id=?",
                (instance_id,),
            )

        response = client.post(
            f"/v1/instances/{instance_id}/power/shutdown",
            headers={"Idempotency-Key": "power-shutdown"},
        )
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


def test_reconcile_route_is_idempotent_and_targets_uncertain_operation(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-reconcile-contract"},
            json=public_body("reconcile-contract"),
        )
        instance_id = created.json()["instance_id"]
        source_id = created.json()["operation_id"]
        with database() as db:
            db.execute(
                "UPDATE operations SET status='uncertain', error='POWER_OPERATION_UNCERTAIN' WHERE operation_id=?",
                (source_id,),
            )
            db.execute(
                "UPDATE instances SET status='uncertain', vmid=40000, power_state='unknown' WHERE instance_id=?",
                (instance_id,),
            )

        first = client.post(
            f"/v1/instances/{instance_id}/reconcile",
            headers={"Idempotency-Key": "reconcile-contract"},
        )
        replay = client.post(
            f"/v1/instances/{instance_id}/reconcile",
            headers={"Idempotency-Key": "reconcile-contract"},
        )
        assert first.status_code == replay.status_code == 202
        assert first.json() == replay.json()
        assert first.json()["command"] == "reconcile"
        assert first.json()["source_operation_id"] == source_id
        assert "/v1/instances/{instance_id}/reconcile" in client.get("/openapi.json").json()["paths"]


def test_power_state_reports_missing_permission_without_hiding_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-power-read"},
            json=public_body("power-read"),
        )
        instance_id = created.json()["instance_id"]
        with database() as db:
            db.execute(
                "UPDATE instances SET status='ready', vmid=40001 WHERE instance_id=?",
                (instance_id,),
            )

        class FakeProxmox:
            def permissions(self, node, vmid, *, mutate=False):
                if mutate:
                    raise ProxmoxError("POWER_PERMISSION_DENIED", 403)

            def power_state(self, node, vmid, name):
                return {
                    "power_state": "stopped",
                    "observed_at": "now",
                    "qmp_status": "stopped",
                    "uptime": 0,
                }

        monkeypatch.setattr("api.main.ProxmoxClient", FakeProxmox)
        response = client.get(f"/v1/instances/{instance_id}/power")
        assert response.status_code == 200
        assert response.json()["power_state"] == "stopped"
        assert response.json()["mutations_available"] is False
        assert response.json()["blocked_reason"] == "POWER_PERMISSION_DENIED"


def test_delete_is_idempotent_and_keeps_the_public_job_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-delete"},
            json=public_body("delete-contract"),
        )
        instance_id = created.json()["instance_id"]
        provision_id = created.json()["operation_id"]
        with database() as db:
            db.execute("UPDATE operations SET status='succeeded' WHERE operation_id=?", (provision_id,))
            db.execute(
                "UPDATE instances SET status='ready', vmid=40002, power_state='stopped' WHERE instance_id=?",
                (instance_id,),
            )

        first = client.delete(
            f"/v1/instances/{instance_id}",
            headers={"Idempotency-Key": "delete-contract"},
        )
        replay = client.delete(
            f"/v1/instances/{instance_id}",
            headers={"Idempotency-Key": "delete-contract"},
        )
        assert first.status_code == replay.status_code == 202
        assert first.json() == replay.json()
        assert first.json()["status"] == "queued"
        assert first.json()["job_id"] == first.json()["operation_id"]


@pytest.mark.parametrize("protected_vmid", [104, 106, 29000])
def test_protected_vms_reject_public_mutations_without_queueing(tmp_path, monkeypatch, protected_vmid):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": f"protected-create-{protected_vmid}"},
            json=public_body(f"protected-{protected_vmid}"),
        )
        assert created.status_code == 202
        instance_id = created.json()["instance_id"]
        source_id = created.json()["operation_id"]
        with database() as db:
            db.execute("UPDATE operations SET status='succeeded' WHERE operation_id=?", (source_id,))
            db.execute(
                "UPDATE instances SET status='ready', vmid=?, power_state='stopped' WHERE instance_id=?",
                (protected_vmid, instance_id),
            )

        responses = [
            client.post(f"/v1/instances/{instance_id}/provision", json={}),
            client.delete(
                f"/v1/instances/{instance_id}",
                headers={"Idempotency-Key": f"protected-delete-{protected_vmid}"},
            ),
            client.post(
                f"/v1/instances/{instance_id}/power/start",
                headers={"Idempotency-Key": f"protected-power-{protected_vmid}"},
            ),
        ]
        assert [(response.status_code, response.json()["detail"]) for response in responses] == [
            (409, "PROTECTED_INSTANCE"),
            (409, "PROTECTED_INSTANCE"),
            (409, "PROTECTED_INSTANCE"),
        ]

        with database() as db:
            db.execute("UPDATE operations SET status='uncertain' WHERE operation_id=?", (source_id,))
            db.execute("UPDATE instances SET status='uncertain' WHERE instance_id=?", (instance_id,))
        reconcile = client.post(
            f"/v1/instances/{instance_id}/reconcile",
            headers={"Idempotency-Key": f"protected-reconcile-{protected_vmid}"},
        )
        assert reconcile.status_code == 409
        assert reconcile.json()["detail"] == "PROTECTED_INSTANCE"

        with database() as db:
            operations = db.execute(
                "SELECT COUNT(*) AS count FROM operations WHERE instance_id=?", (instance_id,)
            ).fetchone()["count"]
            instance = db.execute(
                "SELECT status, vmid FROM instances WHERE instance_id=?", (instance_id,)
            ).fetchone()
        assert operations == 1
        assert (instance["status"], instance["vmid"]) == ("uncertain", protected_vmid)


def test_failed_instances_are_hidden_from_active_listing_and_can_be_logically_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "create-failed-cleanup"},
            json=public_body("failed-cleanup"),
        )
        instance_id = created.json()["instance_id"]
        operation_id = created.json()["operation_id"]
        with database() as db:
            db.execute("UPDATE operations SET status='failed' WHERE operation_id=?", (operation_id,))
            db.execute(
                "UPDATE instances SET status='failed', vmid=NULL, power_state='unknown' WHERE instance_id=?",
                (instance_id,),
            )

        active = client.get("/v1/instances")
        assert active.status_code == 200
        assert active.json()["total"] == 0
        failed = client.get("/v1/instances?status=failed")
        assert failed.status_code == 200
        assert failed.json()["total"] == 1

        deleted = client.delete(
            f"/v1/instances/{instance_id}",
            headers={"Idempotency-Key": "destroy-failed-cleanup"},
        )
        assert deleted.status_code == 202
        assert deleted.json()["status"] == "completed"
        detail = client.get(f"/v1/instances/{instance_id}")
        assert detail.json()["status"] == "deleted"
        operation = client.get(f"/v1/operations/{deleted.json()['operation_id']}")
        assert operation.json()["status"] == "completed"
        assert "No había VM física" in operation.json()["output"]


def test_observability_marks_workers_stale_by_heartbeat_age(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_WORKER_STALE_SECONDS", "30")
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with TestClient(app) as client:
        with database() as db:
            db.execute(
                "INSERT INTO workers(worker_id, status, last_heartbeat, current_operation_id) VALUES (?, ?, ?, NULL)",
                ("worker-stale", "running", old),
            )
        response = client.get("/v1/observability")
        assert response.status_code == 200
        body = response.json()
        assert body["worker_liveness"] == "stale"
        worker = next(item for item in body["workers"] if item["worker_id"] == "worker-stale")
        assert worker["liveness"] == "stale"
        assert worker["heartbeat_stale"] is True
        assert worker["heartbeat_age_seconds"] > 30


def test_configured_password_policy_rejects_short_root_password(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_ROOT_PASSWORD_MIN_LENGTH", "12")
    body = public_body("password-policy", password_only=True)
    body["access"]["root_password"] = "too-short"
    with TestClient(app) as client:
        response = client.post("/v1/instances", json=body)
    assert response.status_code == 422
    assert "root_password" in response.text


def test_queue_limit_returns_retryable_429(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_MAX_QUEUE_DEPTH", "1")
    with TestClient(app) as client:
        first = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "queue-first"},
            json=public_body("queue-first"),
        )
        second = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "queue-second"},
            json=public_body("queue-second"),
        )
    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers["retry-after"] == "5"


def test_console_is_available_without_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    with TestClient(app) as client:
        response = client.get("/console")
        assert response.status_code == 200
        assert "Crear y administrar" in response.text
        assert "Aplicar plan" not in response.text


def test_initialize_migrates_existing_postgresql_schema():
    with database() as db:
        db.execute(
            """
        CREATE TABLE operations (
            operation_id TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            worker_id TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            last_heartbeat TEXT,
            exit_code INTEGER,
            output TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        )
        """
        )
        db.execute(
            """
        CREATE TABLE instances (
            instance_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            name TEXT NOT NULL,
            image TEXT NOT NULL,
            vmid INTEGER,
            node TEXT NOT NULL DEFAULT 'mng01',
            status TEXT NOT NULL,
            power_state TEXT,
            spec_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
        )
        db.execute(
            "CREATE TABLE events (event_id BIGSERIAL PRIMARY KEY, operation_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL, occurred_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')"
        )
        db.execute(
            "CREATE TABLE workers (worker_id TEXT PRIMARY KEY, status TEXT NOT NULL, last_heartbeat TEXT NOT NULL, current_operation_id TEXT)"
        )
        db.execute(
            "CREATE TABLE vmid_leases (vmid BIGINT PRIMARY KEY, operation_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, released_at TEXT)"
        )

    initialize()

    with database() as db:
        operation_columns = {
            row["column_name"] for row in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='operations'"
            )
        }
        instance_columns = {
            row["column_name"] for row in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='instances'"
            )
        }
        assert {"action", "plan_id", "instance_id", "plan_operation_id", "stack", "target_node", "secret_path", "commercial_context_json"} <= operation_columns
        assert {"plan_id", "commercial_context_json"} <= instance_columns
        assert db.execute("SELECT to_regclass('idempotency_keys') AS table_name").fetchone()["table_name"]
        assert db.execute("SELECT to_regclass('ip_allocations') AS table_name").fetchone()["table_name"]
        assert db.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"] == 2
