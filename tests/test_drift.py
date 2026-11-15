"""Detección de drift IACP ↔ Proxmox (solo lectura)."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from api.db import database, initialize
from api.drift import drift_report


def _instance(instance_id, name, vmid, status="ready", node="mng01"):
    created = datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, spec_json, created_at, updated_at)
            VALUES (?, ?, ?, 'ubuntu-2404', ?, ?, ?, '{}', ?, ?)
            """,
            (instance_id, f"op-{instance_id}", name, vmid, node, status, created, created),
        )


def test_drift_report_classifies_differences_without_actions(monkeypatch):
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40099")
    initialize()
    _instance("ok", "vm-ok", 40001)
    _instance("missing", "vm-missing", 40002)
    _instance("renamed", "vm-renamed", 40003)
    created = datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute(
            "INSERT INTO vmid_leases(vmid, operation_id, status, created_at) VALUES (40050, 'op-lost', 'held', ?)",
            (created,),
        )
    inventory = [
        {"vmid": 40001, "name": "vm-ok", "node": "mng01"},
        {"vmid": 40003, "name": "cambiada-a-mano", "node": "mng01"},
        {"vmid": 40010, "name": "huerfana", "node": "mng02"},
        {"vmid": 104, "name": "iacp-controlplane-01", "node": "mng01"},
        {"vmid": 39999, "name": "fuera-de-rango", "node": "mng01"},
    ]
    report = drift_report(inventory)
    assert report["status"] == "drift"
    assert report["automatic_actions"] == "none"
    assert [item["vmid"] for item in report["orphaned_in_proxmox"]] == [40010]
    assert [item["instance_id"] for item in report["missing_in_proxmox"]] == ["missing"]
    assert report["identity_mismatch"][0]["observed"]["name"] == "cambiada-a-mano"
    assert [item["vmid"] for item in report["stale_vmid_leases"]] == [40050]


def test_drift_ignores_instances_with_operation_in_progress(monkeypatch):
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40099")
    initialize()
    _instance("creating", "vm-creating", 40005, status="applying")
    created = datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, instance_id, created_at)
            VALUES ('op-creating-active', 'provision', 'running', '{}', 'creating', ?)
            """,
            (created,),
        )
    report = drift_report([])
    assert report["missing_in_proxmox"] == []
    assert report["status"] == "consistent"


def test_drift_endpoint_reports_proxmox_unavailable(monkeypatch):
    import api.main as api_main
    from worker.proxmox import ProxmoxError

    class DownProxmox:
        def inventory(self, *, uncertain=False):
            raise ProxmoxError("Proxmox GET /cluster/resources falló")

    monkeypatch.setattr(api_main, "ProxmoxClient", DownProxmox)
    with TestClient(api_main.app) as client:
        response = client.get("/v1/observability/drift")
    assert response.status_code == 503
    assert response.json()["detail"] == "PROXMOX_UNAVAILABLE"


def test_observability_exposes_queue_latency_and_retries():
    import api.main as api_main

    with TestClient(api_main.app) as client:
        body = client.get("/v1/observability").json()
    for key in ("queue_oldest_pending_seconds", "retried_operations", "held_vmid_leases", "database"):
        assert key in body
