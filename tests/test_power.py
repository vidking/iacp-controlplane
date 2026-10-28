import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from iacp.api.dependencies import get_power_client, get_power_publisher
from iacp.lifecycle import ownership
from iacp.lifecycle.execution import _execute, execute_power
from iacp.lifecycle.proxmox import PowerError
from iacp.models import BOOTSTRAP_TENANT_ID, Instance, Operation, OperationEvent, PowerExecution
from iacp.terraform.config import SOURCE_REVISION
from tests.power_helpers import FakePower, managed_instance
from tests.test_instances import identities


@pytest.fixture
def power(sessions, application, monkeypatch):
    identifier = managed_instance(sessions, BOOTSTRAP_TENANT_ID)
    native, publish = FakePower(), Mock()
    application.dependency_overrides[get_power_client] = lambda: lambda: native
    application.dependency_overrides[get_power_publisher] = lambda: publish
    monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "1")
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    return identifier, native, publish


def request(client, auth, identifier, action="start", key="power-request"):
    return client.post(f"/v1/instances/{identifier}/power/{action}", headers={**auth, "Idempotency-Key": key})


@pytest.mark.parametrize("action", ["start", "shutdown", "reboot"])
def test_power_operations_confirm_task_and_native_result(client, auth, sessions, power, action):
    identifier, native, publisher = power
    native.power = "stopped" if action == "start" else "running"
    accepted = request(client, auth, identifier, action)
    assert accepted.status_code == 202
    body = accepted.json()
    publisher.assert_called_once_with(body["operation_id"])
    execute_power(body["operation_id"], sessions, native)
    execute_power(body["operation_id"], sessions, native)
    operation = client.get('/v1/operations/' + body["operation_id"], headers=auth).json()
    assert operation["status"] == "completed" and operation["power"]["guest_ready"] is None
    assert operation["power"]["power_state"] == ("stopped" if action == "shutdown" else "running")
    assert sum(call[0] == "submit" for call in native.calls) == 1
    assert len([row for row in operation["events"] if row["code"] == "power_completed"]) == 1
    replay = request(client, auth, identifier, action)
    assert replay.json()["operation_id"] == body["operation_id"]
    assert request(client, auth, identifier, "shutdown" if action != "shutdown" else "start").status_code == 409
    instance = client.get('/v1/instances/' + identifier, headers=auth).json()
    assert instance["operation_id"] != body["operation_id"]  # creation remains stable
    assert instance["status"] == "ready"
    observed = client.get(f'/v1/instances/{identifier}/power', headers=auth).json()
    assert observed["guest_ready"] is None and all(observed[k] == "unknown" for k in ("os_boot", "guest_agent", "cloud_init", "accessible"))


def test_power_without_key_generates_one_and_replays_with_returned_key(client, auth, sessions, power):
    identifier, native, publisher = power
    native.power = "stopped"
    accepted = client.post(f"/v1/instances/{identifier}/power/start", headers=auth)
    assert accepted.status_code == 202
    generated = accepted.headers.get("Idempotency-Key")
    assert generated and generated.startswith("http-start-")
    body = accepted.json()
    with sessions() as session:
        operation = session.get(Operation, body["operation_id"])
        assert operation.idempotency_key == generated
    replay = client.post(f"/v1/instances/{identifier}/power/start",
                         headers={**auth, "Idempotency-Key": generated})
    assert replay.status_code == 202 and replay.json() == body
    assert publisher.call_count == 2


@pytest.mark.parametrize("action,state", [("start", "running"), ("shutdown", "stopped")])
def test_already_in_target_state_is_an_audited_noop(client, auth, sessions, power, action, state):
    identifier, native, _ = power
    native.power = state
    body = request(client, auth, identifier, action).json()
    execute_power(body["operation_id"], sessions, native)
    with sessions() as session:
        row = session.get(PowerExecution, body["operation_id"])
        assert row.status == "completed" and row.result["noop"] is True and row.submitted_at is None
    assert not any(call[0] == "submit" for call in native.calls)


def test_stopped_vm_cannot_be_rebooted(client, auth, sessions, power):
    identifier, native, _ = power
    body = request(client, auth, identifier, "reboot").json()
    execute_power(body["operation_id"], sessions, native)
    result = client.get('/v1/operations/' + body["operation_id"], headers=auth).json()
    assert result["status"] == "failed" and result["error_code"] == "POWER_REBOOT_REQUIRES_RUNNING"
    assert not any(call[0] == "submit" for call in native.calls)


@pytest.mark.parametrize("gate", ["unpromoted", "wrong-source", "disabled"])
def test_ownership_and_runtime_gate_fail_closed_without_publishing(client, auth, power, monkeypatch, gate):
    identifier, native, publisher = power
    if gate == "unpromoted": monkeypatch.setattr(ownership, "KVM_POWER_SOURCE_REVISION", None)
    if gate == "wrong-source": monkeypatch.setattr(ownership, "KVM_POWER_SOURCE_REVISION", "f" * 40)
    if gate == "disabled": monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "0")
    assert request(client, auth, identifier).status_code == 409
    publisher.assert_not_called()
    assert not native.calls
    response = client.get(f'/v1/instances/{identifier}/power', headers=auth).json()
    assert response["mutations_available"] is False


@pytest.mark.parametrize("body", [{"vmid": 104}, {"node": "mng02"}, {"forceStop": True}, {"timeout": 0}])
def test_client_cannot_override_target_or_force_flags(client, auth, power, body):
    identifier, native, publisher = power
    result = client.post(f'/v1/instances/{identifier}/power/start', headers={**auth, "Idempotency-Key": "unsafe"}, json=body)
    assert result.status_code == 422
    publisher.assert_not_called()
    assert not native.calls


def test_reader_and_other_tenant_cannot_mutate_or_discover_instances(client, identities, sessions, power):
    identifier = managed_instance(sessions, identities["tenant-a-operator"]["tenant_id"], vmid=30002, name="tenant-kvm")
    assert request(client, identities["tenant-a-reader"]["headers"], identifier).status_code == 403
    assert request(client, identities["tenant-b-operator"]["headers"], identifier).status_code == 404
    assert client.get(f'/v1/instances/{identifier}/power', headers=identities["tenant-b-reader"]["headers"]).status_code == 404
    assert client.get(f'/v1/instances/{identifier}/power', headers=identities["tenant-a-reader"]["headers"]).status_code == 200


def test_controlplane_and_unprovisioned_resources_are_excluded(client, auth, sessions, power):
    identifier, native, publisher = power
    with sessions() as session:
        instance = session.get(Instance, identifier)
        instance.vmid = 104
        session.commit()
    assert request(client, auth, identifier).status_code == 409
    assert not native.calls
    publisher.assert_not_called()


def test_broker_failure_can_republish_same_operation_safely(client, auth, sessions, power):
    identifier, native, publisher = power
    publisher.side_effect = TimeoutError("private broker error")
    assert request(client, auth, identifier).status_code == 503
    publisher.side_effect = None
    accepted = request(client, auth, identifier).json()
    execute_power(accepted["operation_id"], sessions, native)
    assert client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()["status"] == "completed"
    assert sum(c[0] == "submit" for c in native.calls) == 1


def test_ambiguous_submit_never_replays_reboot(client, auth, sessions, power, monkeypatch):
    identifier, native, _ = power
    native.power = "running"
    submit = Mock(side_effect=PowerError("POWER_PROXMOX_UNAVAILABLE"))
    monkeypatch.setattr(native, "submit", submit)
    accepted = request(client, auth, identifier, "reboot").json()
    execute_power(accepted["operation_id"], sessions, native)
    execute_power(accepted["operation_id"], sessions, native)
    result = client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()
    assert result["status"] == "uncertain" and result["error_code"] == "POWER_TASK_UNKNOWN"
    assert result["power"]["recovery_required"] is True
    submit.assert_called_once()
    assert request(client, auth, identifier, "shutdown", key="another").status_code == 409


def test_worker_interruption_after_task_persistence_resumes_observation(client, auth, sessions, power, monkeypatch):
    identifier, native, _ = power
    accepted = request(client, auth, identifier).json()
    monkeypatch.setattr(native, "task_status", Mock(side_effect=SystemExit("worker killed")))
    with pytest.raises(SystemExit):
        execute_power(accepted["operation_id"], sessions, native)
    with sessions() as session:
        row = session.get(PowerExecution, accepted["operation_id"])
        assert row.task_id and row.status == "running"
    monkeypatch.setattr(native, "task_status", lambda *args: "completed")
    execute_power(accepted["operation_id"], sessions, native)
    assert client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()["status"] == "completed"
    assert sum(c[0] == "submit" for c in native.calls) == 1


def test_shutdown_timeout_never_forces_or_repeats_shutdown(client, auth, sessions, power):
    identifier, native, _ = power
    native.power, native.outcome = "running", "running"
    accepted = request(client, auth, identifier, "shutdown").json()
    execute_power(accepted["operation_id"], sessions, native, monitor_seconds=0)
    result = client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()
    assert result["status"] == "uncertain" and result["error_code"] == "POWER_TASK_TIMEOUT"
    native.power, native.outcome = "running", "timeout"
    execute_power(accepted["operation_id"], sessions, native)
    result = client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()
    assert result["status"] == "failed" and result["error_code"] == "POWER_ACPI_TIMEOUT"
    assert sum(c[0] == "submit" for c in native.calls) == 1


def test_revoking_ownership_before_dispatch_stops_mutation(client, auth, sessions, power, monkeypatch):
    identifier, native, _ = power
    accepted = request(client, auth, identifier).json()
    monkeypatch.setattr(ownership, "KVM_POWER_SOURCE_REVISION", None)
    execute_power(accepted["operation_id"], sessions, native)
    result = client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()
    assert result["status"] == "failed" and result["error_code"] == "POWER_OWNERSHIP_UNAVAILABLE"
    assert not any(c[0] == "submit" for c in native.calls)


def test_lost_lock_cannot_commit_completion(client, auth, sessions, power, monkeypatch):
    identifier, native, _ = power
    accepted = request(client, auth, identifier).json()
    lost = False
    def heartbeat():
        if lost:
            raise RuntimeError("Instance lock connection lost")
    def completed(*args):
        nonlocal lost
        lost = True
        return "completed"
    monkeypatch.setattr(native, "task_status", completed)
    with pytest.raises(RuntimeError, match="lock connection lost"):
        _execute(accepted["operation_id"], sessions, native, heartbeat, 90, lambda _: None)
    with sessions() as session:
        assert session.get(PowerExecution, accepted["operation_id"]).status == "running"
        assert session.scalar(select(OperationEvent.id).where(OperationEvent.code == "power_completed")) is None


def test_legacy_source_cannot_claim_lifecycle_ownership(client, auth, power, monkeypatch):
    identifier, native, publisher = power
    monkeypatch.setattr(ownership.config, "SOURCE_REVISION", ownership.LEGACY_TERRAFORM_POWER_SOURCE)
    monkeypatch.setattr(ownership, "KVM_POWER_SOURCE_REVISION", ownership.LEGACY_TERRAFORM_POWER_SOURCE)
    assert request(client, auth, identifier).json()["detail"] == "POWER_OWNERSHIP_UNAVAILABLE"
    assert not native.calls
    publisher.assert_not_called()


@pytest.mark.parametrize("action", ["start", "shutdown", "reboot"])
def test_task_success_with_wrong_final_power_keeps_uncertain(client, auth, sessions, power, monkeypatch, action):
    identifier, native, _ = power
    initial = "stopped" if action == "start" else "running"
    native.power = initial
    original = native.submit
    def submitted(*args):
        task = original(*args)
        native.power = initial if action != "reboot" else "stopped"
        return task
    monkeypatch.setattr(native, "submit", submitted)
    accepted = request(client, auth, identifier, action).json()
    execute_power(accepted["operation_id"], sessions, native, monitor_seconds=0)
    result = client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()
    assert result["status"] == "uncertain" and result["error_code"] == "POWER_RESULT_MISMATCH"


def test_reboot_waits_for_qemu_restart_after_task_completion(client, auth, sessions, power, monkeypatch):
    from iacp.lifecycle.proxmox import readiness
    identifier, native, _ = power
    native.power = "running"
    observed = Mock(side_effect=[readiness("running"), readiness("stopped"), readiness("running")])
    monkeypatch.setattr(native, "observe", observed)
    accepted = request(client, auth, identifier, "reboot").json()
    execute_power(accepted["operation_id"], sessions, native, monitor_seconds=1, pause=lambda _: None)
    result = client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()
    assert result["status"] == "completed" and observed.call_count == 3
    assert sum(call[0] == "submit" for call in native.calls) == 1


def test_reboot_timeout_is_completed_only_after_observed_uptime_reset(client, auth, sessions, power):
    identifier, native, _ = power
    native.power = "running"
    native.outcome = "timeout"
    native.uptime = Mock(side_effect=[500, 7])
    accepted = request(client, auth, identifier, "reboot", key="reboot-timeout-observed")
    execute_power(accepted.json()["operation_id"], sessions, native, pause=lambda _: None)
    result = client.get('/v1/operations/' + accepted.json()["operation_id"], headers=auth).json()
    assert result["status"] == "completed"
    assert result["error_code"] is None
    assert sum(call[0] == "submit" for call in native.calls) == 1


@pytest.mark.parametrize("change", ["simulated", "not-ready", "missing-evidence"])
def test_only_completed_managed_kvm_can_accept_power(client, auth, sessions, power, change):
    from iacp.models import TerraformApply
    identifier, native, publisher = power
    with sessions() as session:
        if change == "simulated": session.get(Instance, identifier).executor = "simulated"
        if change == "not-ready": session.get(Instance, identifier).status = "provisioning"
        if change == "missing-evidence":
            session.scalar(select(TerraformApply).where(TerraformApply.instance_id == identifier)).result = {}
        session.commit()
    assert request(client, auth, identifier).status_code == 409
    publisher.assert_not_called()
    assert not native.calls


def test_openapi_has_four_authenticated_power_contracts_and_no_force_parameters():
    from iacp.main import create_app
    schema = create_app().openapi()
    prefix = '/v1/instances/{instance_id}/power'
    for path, method in [(prefix, 'get')] + [(prefix + '/' + action, 'post') for action in ('start', 'shutdown', 'reboot')]:
        contract = schema['paths'][path][method]
        assert contract['security'] == [{'HTTPBearer': []}]
        if method == 'post':
            assert '202' in contract['responses']
            assert any(item['name'] == 'Idempotency-Key' and not item['required'] for item in contract['parameters'])
            assert contract['responses']['202']['headers']['Idempotency-Key']['schema']['type'] == 'string'
            assert not contract.get('requestBody', {}).get('required', False)
    assert schema['components']['schemas']['PowerRequest']['additionalProperties'] is False
    assert schema['components']['schemas']['PowerRequest']['properties'] == {}


def test_lifecycle_compose_uses_a_dedicated_queue_and_no_state_or_terraform_credentials():
    from pathlib import Path
    import yaml
    compose = yaml.safe_load(Path('compose.lifecycle.yaml').read_text())
    worker = compose['services']['lifecycle-worker']
    assert worker['secrets'] == ['db_password', 'kvm_credentials']
    assert not worker.get('volumes')
    assert worker['read_only'] is True and worker['cap_drop'] == ['ALL']
    assert 'lifecycle' in worker['command']
    assert worker['environment']['IACP_KVM_LIFECYCLE_ENABLED'] == '${IACP_KVM_LIFECYCLE_ENABLED:-0}'
