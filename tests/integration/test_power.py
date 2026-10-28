import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
import time
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import sessionmaker

from iacp.api.dependencies import get_power_publisher
from iacp.lifecycle import ownership
from iacp.lifecycle.execution import execute_power
from iacp.models import BOOTSTRAP_TENANT_ID, OperationEvent, PowerExecution
from iacp.terraform.locks import ExecutionBusy, instance_lock
from tests.helpers import upgrade
from tests.power_fixture import power_server
from tests.power_helpers import managed_instance

pytestmark = pytest.mark.integration


@pytest.fixture
def sessions(pg_engine):
    upgrade(pg_engine)
    return sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def power_runtime(sessions, application, monkeypatch, tmp_path):
    identifier = managed_instance(sessions, BOOTSTRAP_TENANT_ID)
    monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "1")
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    application.dependency_overrides[get_power_publisher] = lambda: Mock()
    with power_server(tmp_path, vmid=30001, name="fixture-kvm") as native:
        credentials = tmp_path / "kvm.json"
        credentials.write_text(json.dumps({"endpoint": native.endpoint, "api_token": "synthetic-token"}))
        monkeypatch.setenv("IACP_KVM_CREDENTIALS_FILE", str(credentials))
        monkeypatch.setenv("IACP_KVM_CA_FILE", str(native.ca_file))
        yield identifier, native


def post(client, auth, identifier, action="start", key="integration-power"):
    return client.post(f'/v1/instances/{identifier}/power/{action}', headers={**auth, "Idempotency-Key": key})


@pytest.mark.parametrize("action", ["start", "shutdown", "reboot"])
def test_https_native_tasks_with_postgres_and_no_guest_agent(client, auth, sessions, power_runtime, action):
    identifier, native = power_runtime
    native.power = "stopped" if action == "start" else "running"
    accepted = post(client, auth, identifier, action)
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    execute_power(body["operation_id"], sessions)
    execute_power(body["operation_id"], sessions)
    operation = client.get('/v1/operations/' + body["operation_id"], headers=auth).json()
    assert operation["status"] == "completed" and operation["power"]["guest_ready"] is None
    assert native.posts == 1
    assert not any('/agent/' in path or '/stop' in path or '/reset' in path for _, path, _ in native.requests)
    live = client.get(f'/v1/instances/{identifier}/power', headers=auth).json()
    assert live["power_state"] == ("stopped" if action == "shutdown" else "running")
    assert live["guest_ready"] is None and live["guest_agent"] == "unknown"


def test_two_workers_share_the_terraform_instance_lock(client, auth, sessions, power_runtime, pg_engine):
    identifier, native = power_runtime
    native.task_running = True
    accepted = post(client, auth, identifier).json()
    errors = []
    def first():
        try:
            execute_power(accepted["operation_id"], sessions)
        except BaseException as failure:
            errors.append(failure)
    worker = Thread(target=first)
    worker.start()
    try:
        assert native.task_seen.wait(10)
        second = Mock()
        execute_power(accepted["operation_id"], sessions, second)
        assert not second.mock_calls
        with pytest.raises(ExecutionBusy):
            with instance_lock(pg_engine, identifier):
                pass
        assert post(client, auth, identifier, "shutdown", key="other-operation").status_code == 409
        assert post(client, auth, identifier).json()["operation_id"] == accepted["operation_id"]
    finally:
        native.task_running = False
        worker.join(15)
    assert not worker.is_alive() and not errors and native.posts == 1
    with sessions() as session:
        assert session.get(PowerExecution, accepted["operation_id"]).status == "completed"
        assert len(session.scalars(select(OperationEvent).where(OperationEvent.code == "power_completed")).all()) == 1


def test_0005_preserves_existing_instances_and_history(pg_engine):
    upgrade(pg_engine, "0004_authorized_apply")
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    managed_instance(factory, BOOTSTRAP_TENANT_ID)
    tables = [name for name in inspect(pg_engine).get_table_names() if name != "alembic_version"]
    with pg_engine.connect() as connection:
        before = {name: connection.execute(text(f'SELECT * FROM "{name}"')).all() for name in tables}
    upgrade(pg_engine, "0005_kvm_lifecycle")
    upgrade(pg_engine, "0005_kvm_lifecycle")
    with pg_engine.connect() as connection:
        assert {name: connection.execute(text(f'SELECT * FROM "{name}"')).all() for name in tables} == before
    assert set(inspect(pg_engine).get_table_names()) - set(tables) == {"power_executions", "alembic_version"}


def test_real_celery_worker_interruption_redelivery_and_api_restart(
        client, auth, sessions, power_runtime, runtime, application):
    from iacp.lifecycle.worker import celery_app
    identifier, native = power_runtime
    native.task_running = True
    application.dependency_overrides.pop(get_power_publisher)
    celery_app.conf.broker_url = os.environ["CELERY_BROKER_URL"]
    child_env = {**os.environ, "PYTHONPATH": "/workspace/app:/workspace"}
    command = [sys.executable, "-m", "celery", "-A", "tests.lifecycle_worker:celery_app", "worker", "-Q", "lifecycle",
               "--pool=solo", "--concurrency=1", "--loglevel=WARNING", "--without-gossip", "--without-mingle", "--without-heartbeat"]
    worker = subprocess.Popen(command, env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        accepted = post(client, auth, identifier).json()
        assert native.task_seen.wait(15)
        with sessions() as session:
            assert session.get(PowerExecution, accepted["operation_id"]).task_id is not None
        worker.kill()
        worker.wait(timeout=5)
        native.task_running = False
        worker = subprocess.Popen(command, env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        replay = post(client, auth, identifier)
        assert replay.status_code == 202 and replay.json()["operation_id"] == accepted["operation_id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            operation = client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()
            if operation["status"] == "completed":
                break
            assert worker.poll() is None
            time.sleep(.1)
        else:
            pytest.fail("Lifecycle observation did not resume after worker interruption")
        assert native.posts == 1
        assert len([item for item in operation["events"] if item["code"] == "power_completed"]) == 1
        # A fresh API process can still retrieve the same durable operation.
        from iacp.main import create_app
        with TestClient(create_app()) as restarted:
            assert restarted.get('/v1/operations/' + accepted["operation_id"], headers=auth).json() == operation
    finally:
        worker.terminate()
        worker.wait(timeout=10)
        celery_app.close()


def test_terraform_ignore_changes_preserves_creation_and_only_ignores_updates(tmp_path):
    """Terraform core contract for the proposed bool ownership handoff.

    Uses only the built-in terraform_data provider and wholly synthetic local
    state. No apply/import/destroy and no access to a real backend or VM.
    """
    binary = shutil.which("terraform")
    assert binary
    configuration = '''resource "terraform_data" "owned" {
  input = false
  lifecycle { ignore_changes = [input] }
}
resource "terraform_data" "declarative" { input = false }
'''
    (tmp_path / "main.tf").write_text(configuration)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1"}
    def run(*args):
        result = subprocess.run([binary, *args], cwd=tmp_path, env=env, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode()
        return result.stdout
    assert json.loads(run('version', '-json'))['terraform_version'] == '1.16.6'
    run('init', '-backend=false', '-input=false', '-no-color')
    run('plan', '-refresh=false', '-input=false', '-no-color', '-out=create.tfplan')
    creation = json.loads(run('show', '-json', 'create.tfplan'))
    assert all(change['change']['after']['input'] is False and change['change']['actions'] == ['create']
               for change in creation['resource_changes'])
    state = {"version": 4, "terraform_version": "1.16.6", "lineage": "a27f112f-4a00-4db7-9732-48cd4f30d406",
             "serial": 1, "outputs": {}, "resources": []}
    for name in ('owned', 'declarative'):
        state['resources'].append({"mode": "managed", "type": "terraform_data", "name": name,
            "provider": 'provider["terraform.io/builtin/terraform"]', "instances": [{"schema_version": 0,
            "attributes": {"id": "11111111-1111-4111-8111-111111111111", "input": {"value": True, "type": "bool"},
                           "output": {"value": True, "type": "bool"}, "triggers_replace": None}, "sensitive_attributes": []}]})
    (tmp_path / 'terraform.tfstate').write_text(json.dumps(state))
    run('plan', '-refresh=false', '-input=false', '-no-color', '-out=update.tfplan')
    changes = {item['name']: item['change'] for item in json.loads(run('show', '-json', 'update.tfplan'))['resource_changes']}
    assert changes['owned']['actions'] == ['no-op'] and changes['owned']['after']['input'] is True
    assert changes['declarative']['actions'] == ['update'] and changes['declarative']['after']['input'] is False
