"""PostgreSQL y Celery reales con un adaptador Terraform exclusivo de pruebas."""
import hashlib
import json
import os
import subprocess
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from iacp.api.dependencies import get_instance_publisher
from iacp.db import get_session_factory
from iacp.main import create_app
from iacp.models import Instance, Job, OperationEvent, TerraformApply
from iacp.terraform.apply import execute_apply
from iacp.terraform.locks import instance_lock
from tests.helpers import upgrade
from tests.test_apply import ApplyingExecutor
from tests.test_instances import create
from tests.test_terraform import run_plan

pytestmark = pytest.mark.integration


def test_apply_survives_broker_redelivery_and_api_restart(pg_engine, runtime, monkeypatch, tmp_path):
    upgrade(pg_engine)
    sessions = get_session_factory()
    monkeypatch.setenv("IACP_INSTANCE_EXECUTOR", "terraform")
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    app = create_app()
    app.dependency_overrides[get_instance_publisher] = lambda: lambda identifier: None
    with TestClient(app) as client:
        accepted = create(client, runtime).json()
        run_plan(accepted, sessions)
        with sessions() as session:
            tenant_id = session.get(Instance, accepted["instance_id"]).tenant_id
        token = "integration-approval-" + uuid4().hex
        registry = tmp_path / "approval.json"
        registry.write_text(json.dumps([{"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                        "tenant_id": tenant_id, "subject": "infra-test", "role": "approver"}]))
        monkeypatch.setenv("IACP_TENANT_CREDENTIALS_FILE", str(registry))
        path = f'/v1/instances/{accepted["instance_id"]}/terraform'
        approved = client.post(path + "/approvals", headers={"Authorization": "Bearer " + token, "Idempotency-Key": "approve"},
                               json={"attempt": 1, "plan_sha256": "a" * 64})
        assert approved.status_code == 201
        approval_id = approved.json()["approval_id"]
        queued = client.post(path + "/apply", headers={**runtime, "Idempotency-Key": "apply"}, json={"approval_id": approval_id})
        assert queued.status_code == 202
        fake = ApplyingExecutor()
        with instance_lock(pg_engine, accepted["instance_id"]):
            execute_apply(approval_id, sessions, fake)
        assert not fake.calls
        worker = subprocess.Popen(["celery", "-A", "tests.terraform_worker:celery_app", "worker", "-Q", "terraform",
                                   "--pool=solo", "--loglevel=ERROR", "--without-gossip", "--without-mingle", "--without-heartbeat"],
                                  env={**os.environ, "PYTHONPATH": "/workspace/app:/workspace"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                response = client.get(path + "/applies", headers=runtime).json()[0]
                if response["status"] in {"completed", "failed", "uncertain"}: break
                time.sleep(.1)
            assert response["status"] == "completed", response
            execute_apply(approval_id, sessions, fake)
            assert not fake.calls
            with TestClient(create_app()) as restarted:
                assert restarted.get(path + "/applies", headers=runtime).json()[0] == response
                instance = restarted.get('/v1/instances/' + accepted["instance_id"], headers=runtime).json()
                assert instance["status"] == "ready" and instance["vmid"] == 21001 and instance["result"]["simulated"] is False
            with sessions() as session:
                events = session.scalars(select(OperationEvent).where(OperationEvent.operation_id == accepted["operation_id"],
                                                                      OperationEvent.code == "terraform_mutation_started")).all()
                assert len(events) == 1
        finally:
            worker.terminate()
            worker.wait(timeout=10)


def test_0004_adds_only_apply_table_and_preserves_all_2b_rows(pg_engine):
    from sqlalchemy import inspect, text
    upgrade(pg_engine, "0003_terraform_plans")
    before_tables = inspect(pg_engine).get_table_names()
    with pg_engine.connect() as connection:
        before = {table: connection.execute(text(f'SELECT * FROM "{table}"')).all()
                  for table in before_tables if table != "alembic_version"}
    upgrade(pg_engine)
    upgrade(pg_engine)
    with pg_engine.connect() as connection:
        assert {table: connection.execute(text(f'SELECT * FROM "{table}"')).all() for table in before} == before
        assert set(inspect(pg_engine).get_table_names()) - set(before_tables) == {"terraform_applies"}
