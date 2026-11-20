"""Reconciliar infraestructura con un job de servicio en cola no debe bloquearse."""

import pytest
from fastapi.testclient import TestClient

from api.db import database
from api.main import app
from tests.test_api import public_body


def _instancia_uncertain_con_job(client, job_status: str) -> str:
    response = client.post("/v1/instances", json=public_body(f"guarda-{job_status}"))
    instance_id = response.json()["instance_id"]
    operation_id = response.json()["operation_id"]
    with database() as db:
        db.execute("UPDATE operations SET status='uncertain' WHERE operation_id=?", (operation_id,))
        db.execute("UPDATE instances SET status='uncertain', vmid=40999 WHERE instance_id=?", (instance_id,))
        db.execute(
            "INSERT INTO service_instances VALUES (?, ?, 'hermes', '1', '{}', NULL, 'waiting_vm', NULL, 'x', 'x')",
            (f"svc-{job_status}", instance_id),
        )
        db.execute(
            "INSERT INTO service_jobs(job_id, service_id, status, created_at) VALUES (?, ?, ?, 'x')",
            (f"job-{job_status}", f"svc-{job_status}", job_status),
        )
    return instance_id


@pytest.mark.parametrize(("job_status", "esperado"), [("queued", 202), ("running", 409), ("uncertain", 409)])
def test_reconcile_admite_solo_jobs_en_cola(job_status, esperado):
    with TestClient(app) as client:
        instance_id = _instancia_uncertain_con_job(client, job_status)
        response = client.post(
            f"/v1/instances/{instance_id}/reconcile",
            headers={"Idempotency-Key": f"guarda-{job_status}"},
        )
    assert response.status_code == esperado
    if esperado == 409:
        assert response.json()["detail"] == "SERVICE_OPERATION_ACTIVE"


def test_destroy_sigue_bloqueado_por_job_en_cola():
    with TestClient(app) as client:
        instance_id = _instancia_uncertain_con_job(client, "queued")
        response = client.delete(f"/v1/instances/{instance_id}", headers={"Idempotency-Key": "guarda-destroy"})
    assert response.status_code == 409
