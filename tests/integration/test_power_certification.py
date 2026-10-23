"""Real PostgreSQL, Redis/Celery, HTTPS PVE and S3 protocol fixtures.

Only the offline bpg plan is stubbed here; its orchestration and policy have
separate unit tests and the certified source's real-provider plan matrix.
"""
import json
import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import inspect, select, text

from iacp.api.dependencies import get_power_publisher
from iacp.lifecycle import certify as certification
from iacp.models import BOOTSTRAP_TENANT_ID, PowerOwnershipCertificate
from iacp.terraform.state import S3StateReader, StateSnapshot
from tests.helpers import upgrade
from tests.integration.test_power import sessions
from tests.power_fixture import power_server
from tests.power_helpers import managed_instance
from tests.s3_fixture import versioned_s3
from tests.test_power import request
from tests.test_power_certification import historical, history

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("external_writer", [False, True])
def test_certified_historical_106_with_real_celery_and_unchanged_s3(
        client, auth, historical, sessions, application, runtime, monkeypatch, tmp_path, external_writer, record_property):
    from iacp.lifecycle.worker import celery_app
    expected, executor, runner, fake, planner = historical
    data = json.dumps(runner.state()).encode()
    before = history(sessions)
    with versioned_s3(data, "fixture-iacp-lab", expected.state_key) as store, power_server(
            tmp_path, vmid=106, name="fixture-106") as native:
        expected = expected.model_copy(update={"state": certification.ExpectedState.model_validate(
            StateSnapshot.parse(data, "fixture-version-1").metadata())})
        def reader(backend, env):
            return S3StateReader(backend | {"endpoints": {"s3": store.endpoint}},
                                 {"AWS_ACCESS_KEY_ID": "synthetic", "AWS_SECRET_ACCESS_KEY": "synthetic"})
        monkeypatch.setattr(certification, "S3StateReader", reader)
        if external_writer:
            planner.side_effect = lambda *a: store.external_write(data) or {"actions": ["no-op"]}
        credentials = tmp_path / "kvm.json"
        credentials.write_text(json.dumps({"endpoint": native.endpoint, "api_token": "synthetic-token"}))
        monkeypatch.setenv("IACP_KVM_CREDENTIALS_FILE", str(credentials))
        monkeypatch.setenv("IACP_KVM_CA_FILE", str(native.ca_file))
        if external_writer:
            with pytest.raises(certification.PlanError, match="STATE_CHANGED_DURING_OBSERVATION"):
                certification.certify(expected, sessions, executor, record=True)
            with sessions() as session:
                assert session.scalar(select(PowerOwnershipCertificate)) is None
            assert native.posts == 0 and history(sessions) == before
            return
        certificate = certification.certify(expected, sessions, executor, record=True)
        assert history(sessions) == before and native.posts == 0
        record_property("certificate", json.dumps(certificate, sort_keys=True))
        # Even a certificate cannot bypass the disabled runtime flag.
        assert request(client, auth, expected.instance_id).status_code == 409
        monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "1")
        application.dependency_overrides.pop(get_power_publisher, None)
        celery_app.conf.broker_url = os.environ["CELERY_BROKER_URL"]
        command = [sys.executable, "-m", "celery", "-A", "iacp.lifecycle.worker:celery_app", "worker", "-Q", "lifecycle",
                   "--pool=solo", "--concurrency=1", "--loglevel=WARNING", "--without-gossip", "--without-mingle", "--without-heartbeat"]
        worker = subprocess.Popen(command, env={**os.environ, "PYTHONPATH": "/workspace/app:/workspace"},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            accepted = request(client, auth, expected.instance_id).json()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                operation = client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()
                if operation["status"] == "completed":
                    break
                assert worker.poll() is None
                time.sleep(.1)
            else:
                pytest.fail("Historical certificate did not authorize the real Lifecycle worker")
            assert native.posts == 1 and operation["power"]["guest_ready"] is None
            assert request(client, auth, expected.instance_id).json()["operation_id"] == accepted["operation_id"]
            after = history(sessions)
            assert all(row in after[name] for name, rows in before.items() for row in rows)
            assert store.data == data and store.version == 1
            assert store.requests == [("GET", store.state_path), ("GET", store.state_path)]
            record_property("s3_requests", json.dumps(store.requests))
            record_property("proxmox_posts", native.posts)
            record_property("apply_enabled", os.environ["IACP_TERRAFORM_APPLY_ENABLED"])
        finally:
            worker.terminate()
            worker.wait(timeout=10)
            celery_app.close()


def test_0006_preserves_0005_rows_and_never_backfills_certificates(pg_engine):
    from sqlalchemy.orm import sessionmaker
    from uuid import uuid4
    from iacp.lifecycle.execution import execute_power
    from iacp.lifecycle.ownership import LEGACY_TERRAFORM_POWER_SOURCE, KVM_POWER_SOURCE_REVISION
    from iacp.models import Job, Operation, PowerExecution, TerraformApply, TerraformExecution
    from tests.power_helpers import FakePower
    upgrade(pg_engine, "0005_kvm_lifecycle")
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    identifier = managed_instance(factory, BOOTSTRAP_TENANT_ID)
    operation_id = str(uuid4())
    with factory() as session:
        session.get(TerraformExecution, identifier).source_revision = LEGACY_TERRAFORM_POWER_SOURCE
        approval = session.scalar(select(TerraformApply))
        session.add(Operation(id=operation_id, instance_id=identifier, tenant_id=BOOTSTRAP_TENANT_ID,
            kind="instance.power.start", status="queued", idempotency_key="pre-0006", request_hash="a" * 64, requested_by="fixture"))
        session.flush()
        session.add(Job(id=str(uuid4()), operation_id=operation_id, tenant_id=BOOTSTRAP_TENANT_ID,
                        operation="instance.power.start", status="queued"))
        # Use the actual 0005 columns; the model's new nullable FK is absent here.
        session.execute(PowerExecution.__table__.insert().values(operation_id=operation_id, instance_id=identifier,
            action="start", status="queued", phase="queued", vmid=21001, node="mng01", name="fixture-kvm",
            approval_id=approval.id, ownership_revision=KVM_POWER_SOURCE_REVISION))
        session.commit()
    tables = {name: [row["name"] for row in inspect(pg_engine).get_columns(name)]
              for name in inspect(pg_engine).get_table_names() if name != "alembic_version"}
    def snapshot():
        with pg_engine.connect() as connection:
            return {name: connection.execute(text('SELECT ' + ','.join('"' + col + '"' for col in columns)
                + f' FROM "{name}"')).all() for name, columns in tables.items()}
    before = snapshot()
    upgrade(pg_engine)
    upgrade(pg_engine)
    assert snapshot() == before
    with pg_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM power_ownership_certificates")) == 0
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0008_dhcp_profiles"
        assert connection.scalar(text("SELECT ownership_certificate_id FROM power_executions")) is None
    native = FakePower()
    execute_power(operation_id, factory, native)
    assert not native.calls
    with factory() as session:
        assert session.get(Operation, operation_id).error_code == "POWER_OWNERSHIP_CERTIFICATION_REQUIRED"
