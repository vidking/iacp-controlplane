from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Timer
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from iacp.main import create_app
from iacp.models import BOOTSTRAP_TENANT_ID, Instance, Job, Operation, TerraformAttempt, TerraformExecution
from iacp.schemas import InstanceCreate
from iacp.security import Principal
from iacp.services.instances import create_instance
from iacp.services.jobs import QueueUnavailable
from iacp.terraform.actions import retry
from iacp.terraform.lifecycle import execute_plan
from iacp.terraform.locks import ExecutionBusy, instance_lock
from tests.helpers import upgrade
from tests.test_terraform import FakeExecutor, allocation_data

pytestmark = pytest.mark.integration


def test_postgres_lock_rejects_concurrent_worker_and_retry(pg_engine, monkeypatch):
    monkeypatch.setenv("IACP_INSTANCE_EXECUTOR", "terraform")
    upgrade(pg_engine)
    sessions = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    with sessions() as session:
        accepted = create_instance(session, Principal(BOOTSTRAP_TENANT_ID, "test", "operator"),
                                   InstanceCreate(name="concurrent-plan", image="fixture"), "plan-001", lambda job: None)
    started, release = Event(), Event()
    def blocking():
        started.set()
        assert release.wait(10)
    executor = FakeExecutor(callback=blocking)
    from iacp.terraform.config import Allocation
    load = lambda instance: Allocation.model_validate(allocation_data(image="fixture"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(execute_plan, accepted.job_id, 1, sessions, executor, load)
        try:
            assert started.wait(10)
            with pytest.raises(ExecutionBusy):
                with instance_lock(pg_engine, accepted.instance_id):
                    pass
            execute_plan(accepted.job_id, 1, sessions, executor, load)
        finally:
            release.set()
        task.result(timeout=10)
    assert len(executor.calls) == 1
    # El lock de sesión se libera incluso con conexiones recicladas del pool.
    with instance_lock(pg_engine, accepted.instance_id):
        pass
    with sessions() as session:
        assert session.get(TerraformExecution, accepted.instance_id).status == "planned"
        assert len(session.scalars(select(TerraformAttempt)).all()) == 1


def test_0003_preserves_every_2a_entity_and_job(pg_engine):
    from iacp.models import Tenant, OperationEvent
    upgrade(pg_engine, "0002_instances_operations")
    sessions = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    with sessions() as session:
        accepted = create_instance(session, Principal(BOOTSTRAP_TENANT_ID, "legacy-2a", "operator"),
                                   InstanceCreate(name="preserved-2a", image="fixture"), "preserved-2a", lambda job: None)
    models = (Tenant, Instance, Operation, Job, OperationEvent)
    with pg_engine.connect() as connection:
        before = {model.__tablename__: connection.execute(select(model.__table__)).all() for model in models}
    upgrade(pg_engine)
    upgrade(pg_engine)
    with pg_engine.connect() as connection:
        assert {model.__tablename__: connection.execute(select(model.__table__)).all() for model in models} == before


def test_ambiguous_dispatch_and_worker_claim_use_consistent_lock_order(pg_engine, monkeypatch):
    from iacp.terraform.config import Allocation
    monkeypatch.setenv("IACP_INSTANCE_EXECUTOR", "terraform")
    upgrade(pg_engine)
    sessions = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    locked, release = Event(), Event()
    futures = []

    def before_flush(session, context, instances):
        if any(isinstance(row, TerraformExecution) and row.status == "running" for row in session.dirty):
            locked.set()
            assert release.wait(10)

    event.listen(sessions, "before_flush", before_flush)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def publish(job_id):
            futures.append(pool.submit(execute_plan, job_id, 1, sessions, FakeExecutor(),
                                       lambda instance: Allocation.model_validate(allocation_data(image="fixture"))))
            assert locked.wait(10)
            Timer(0.2, release.set).start()
            raise TimeoutError("ambiguous acknowledgement")
        try:
            with sessions() as session:
                with pytest.raises(QueueUnavailable):
                    create_instance(session, Principal(BOOTSTRAP_TENANT_ID, "test", "operator"),
                                    InstanceCreate(name="ambiguous-plan", image="fixture"), "ambiguous-plan", publish)
            futures[0].result(timeout=10)
        finally:
            release.set()
            event.remove(sessions, "before_flush", before_flush)
    with sessions() as session:
        assert session.scalar(select(TerraformExecution.status)) == "planned"
        assert session.scalar(select(Job.status)) == "completed"


def test_celery_terraform_queue_persists_a_simulated_plan_and_recovers_api(pg_engine, runtime, tmp_path, monkeypatch):
    from iacp.terraform.worker import celery_app
    monkeypatch.setenv("IACP_INSTANCE_EXECUTOR", "terraform")
    upgrade(pg_engine)
    allocations = tmp_path / "allocations.json"
    allocations.write_text(json.dumps({f"{BOOTSTRAP_TENANT_ID}/queue-plan": allocation_data(image="fixture")}))
    monkeypatch.setenv("IACP_TERRAFORM_ALLOCATIONS_FILE", str(allocations))
    celery_app.conf.broker_url = os.environ["CELERY_BROKER_URL"]
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": f"{root / 'app'}:{root}"}
    with (tmp_path / "terraform-worker.log").open("w+") as log:
        worker = subprocess.Popen([
            sys.executable, "-m", "celery", "-A", "tests.terraform_worker:celery_app", "worker", "-Q", "terraform",
            "--pool=solo", "--concurrency=1", "--loglevel=WARNING", "--without-gossip", "--without-mingle", "--without-heartbeat",
        ], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            with TestClient(create_app()) as client:
                accepted = client.post("/v1/instances", headers={**runtime, "Idempotency-Key": "queued-plan"},
                                       json={"name": "queue-plan", "image": "fixture"})
                assert accepted.status_code == 202
                body = accepted.json()
                path = f'/v1/instances/{body["instance_id"]}/terraform'
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    response = client.get(path, headers=runtime)
                    assert response.status_code == 200
                    if response.json()["status"] == "planned":
                        break
                    assert worker.poll() is None
                    time.sleep(0.1)
                else:
                    pytest.fail("The real Celery worker did not complete the simulated plan")
                assert response.json()["apply_enabled"] is False
                assert client.get(f'/v1/instances/{body["instance_id"]}', headers=runtime).json()["status"] == "pending"
            with TestClient(create_app()) as restarted:
                assert restarted.get(path, headers=runtime).json() == response.json()
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
            celery_app.close()
