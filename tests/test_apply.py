from datetime import timedelta
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from iacp.api.dependencies import get_apply_publisher
from iacp.models import Instance, Job, TerraformApply, TerraformExecution, utcnow
from iacp.terraform.apply import execute_apply
from iacp.terraform.config import Allocation, PlanError
from iacp.terraform.executor import ADDRESS, TerraformExecutor, observed_resource
from iacp.terraform.locks import instance_lock
from iacp.terraform.runner import CommandRunner
from tests.test_instances import SPEC, create, identities
from tests.test_terraform import allocation_data, plan_json, run_plan, terraform_mode


class ApplyingExecutor:
    def __init__(self, failure=None, callback=None, before_failure=False):
        self.calls = []
        self.failure, self.callback, self.before_failure = failure, callback, before_failure

    def prepare_apply(self, *args):
        pass

    def prepare_reconcile(self, identifier, attempt, sha, *args):
        return {"plan_sha256": sha, "legacy_manifest": False, "execution_enabled": False}

    def apply(self, identifier, attempt, sha, key, allocation, variables, cancelled, before_mutation):
        self.calls.append(identifier)
        if not self.before_failure:
            before_mutation()
        if self.callback:
            self.callback()
        if cancelled():
            raise PlanError("CANCELLED")
        if self.failure:
            raise self.failure if isinstance(self.failure, PlanError) else PlanError(self.failure)
        return self.observe(identifier, attempt, allocation, variables, cancelled)

    def observe(self, identifier, attempt, allocation, variables, cancelled):
        if self.failure:
            raise self.failure if isinstance(self.failure, PlanError) else PlanError(self.failure)
        values = plan_json(variables)["resource_changes"][0]["change"]["after"]
        values.update(started=False, id=str(values["vm_id"]))
        return observed_resource({"root_module": {"child_modules": [{"resources": [
            {"address": ADDRESS, "values": values}]}]}}, variables)


@pytest.fixture
def prepared(client, auth, sessions, terraform_mode, application, monkeypatch, tmp_path):
    accepted = create(client, auth).json()
    run_plan(accepted, sessions)
    token = "approval-test-token-" + "a" * 40
    registry = tmp_path / "credentials.json"
    with sessions() as session:
        tenant = session.get(Instance, accepted["instance_id"]).tenant_id
    registry.write_text(json.dumps([{"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                    "tenant_id": tenant, "subject": "infra-review", "role": "approver"}]))
    monkeypatch.setenv("IACP_TENANT_CREDENTIALS_FILE", str(registry))
    application.dependency_overrides[get_apply_publisher] = lambda: Mock()
    return accepted, {"Authorization": "Bearer " + token}, f'/v1/instances/{accepted["instance_id"]}/terraform'


def approval(client, prepared, headers=None, body=None, key="approve-1"):
    accepted, approver, path = prepared
    return client.post(path + "/approvals", headers={**(headers or approver), "Idempotency-Key": key},
                       json=body or {"attempt": 1, "plan_sha256": "a" * 64})


def dispatch(client, prepared, auth, row, key="apply-1"):
    return client.post(prepared[2] + "/apply", headers={**auth, "Idempotency-Key": key},
                       json={"approval_id": row["approval_id"]})


def test_double_gate_and_separation_of_duties(client, auth, prepared, sessions, monkeypatch):
    assert approval(client, prepared, auth).status_code == 403
    row = approval(client, prepared).json()
    assert row["status"] == "approved" and row["job_id"] is None
    assert dispatch(client, prepared, auth, row).status_code == 503
    assert dispatch(client, prepared, prepared[1], row).status_code == 403
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    queued = dispatch(client, prepared, auth, row)
    assert queued.status_code == 202
    monkeypatch.delenv("IACP_TERRAFORM_APPLY_ENABLED")
    fake = ApplyingExecutor()
    execute_apply(row["approval_id"], sessions, fake)
    assert not fake.calls
    current = client.get(prepared[2] + "/applies", headers=auth).json()[0]
    assert current["status"] == "failed" and current["started_at"] is None


def test_authorization_and_dispatch_are_idempotent(client, auth, prepared, application, monkeypatch, sessions):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    published = Mock()
    application.dependency_overrides[get_apply_publisher] = lambda: published
    row = approval(client, prepared).json()
    assert approval(client, prepared).json() == row
    changed = {"attempt": 1, "plan_sha256": "a" * 64, "valid_for_seconds": 60}
    assert approval(client, prepared, body=changed).status_code == 409
    response = dispatch(client, prepared, auth, row)
    assert response.status_code == 202
    assert dispatch(client, prepared, auth, row).json() == response.json()
    assert dispatch(client, prepared, auth, row, key="different").status_code == 409
    assert published.call_count == 1
    fake = ApplyingExecutor()
    execute_apply(row["approval_id"], sessions, fake)
    execute_apply(row["approval_id"], sessions, fake)
    assert len(fake.calls) == 1
    instance = client.get('/v1/instances/' + prepared[0]["instance_id"], headers=auth).json()
    assert instance["status"] == "ready" and instance["vmid"] == 21001
    assert instance["node"] == "mng01" and instance["infrastructure_kind"] == "kvm"
    assert instance["result"]["simulated"] is False and instance["result"]["power_state"] == "stopped"
    assert client.get('/v1/jobs/' + response.json()["job_id"], headers=auth).json()["status"] == "completed"
    assert client.get('/v1/operations/' + prepared[0]["operation_id"], headers=auth).json()["status"] == "completed"
    assert client.post(prepared[2] + "/retry", headers={**auth, "Idempotency-Key": "bad-retry"}, json={"expected_attempt": 1}).status_code == 409
    assert client.post(prepared[2] + "/cancel", headers=auth).status_code == 409


@pytest.mark.parametrize("failure", ["TERRAFORM_TIMEOUT", "TERRAFORM_COMMAND_FAILED", "RESOURCE_ABSENT", "TERRAFORM_EXECUTION_FAILED"])
def test_uncertain_apply_requires_observation_never_reapplication(client, auth, prepared, monkeypatch, sessions, failure):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared).json()
    dispatch(client, prepared, auth, row)
    fake = ApplyingExecutor(failure)
    execute_apply(row["approval_id"], sessions, fake)
    current = client.get(prepared[2] + "/applies", headers=auth).json()[0]
    assert current["status"] == "uncertain" and current["error_code"] == failure
    execute_apply(row["approval_id"], sessions, fake)
    assert len(fake.calls) == 1
    observer = ApplyingExecutor()
    execute_apply(row["approval_id"], sessions, observer, reconcile=True)
    assert not observer.calls
    assert client.get(prepared[2] + "/applies", headers=auth).json()[0]["status"] == "completed"


def test_expired_or_changed_plan_cannot_execute(client, auth, prepared, sessions, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    assert approval(client, prepared, body={"attempt": 2, "plan_sha256": "a" * 64}).status_code == 409
    row = approval(client, prepared).json()
    with sessions() as session:
        session.get(TerraformApply, row["approval_id"]).expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert dispatch(client, prepared, auth, row).status_code == 409


def test_cancel_and_concurrent_delivery_are_safe(client, auth, prepared, sessions, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared).json()
    dispatch(client, prepared, auth, row)
    fake = ApplyingExecutor()
    with instance_lock(sessions.kw["bind"], prepared[0]["instance_id"]):
        execute_apply(row["approval_id"], sessions, fake)
    assert not fake.calls
    path = prepared[2] + "/applies/" + row["approval_id"]
    assert client.post(path + "/cancel", headers=auth).json()["status"] == "cancelled"
    execute_apply(row["approval_id"], sessions, fake)
    assert not fake.calls


def test_cancel_during_apply_is_uncertain(client, auth, prepared, sessions, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared).json()
    dispatch(client, prepared, auth, row)
    path = prepared[2] + "/applies/" + row["approval_id"] + "/cancel"
    execute_apply(row["approval_id"], sessions, ApplyingExecutor(callback=lambda: client.post(path, headers=auth)))
    assert client.get(prepared[2] + "/applies", headers=auth).json()[0]["status"] == "uncertain"
    instance = client.get("/v1/instances/" + prepared[0]["instance_id"], headers=auth).json()
    assert instance["status"] == "failed" and instance["result"]["error_code"] == "RECONCILIATION_REQUIRED"


def test_tenant_and_reader_cannot_access_or_approve(client, auth, prepared, identities):
    other = identities["tenant-b-operator"]["headers"]
    reader = identities["tenant-a-reader"]["headers"]
    for headers, status in ((other, 404), (reader, 404)):
        assert client.get(prepared[2] + "/applies", headers=headers).status_code == status
        assert approval(client, prepared, headers).status_code == 403


def test_runner_uses_only_fixed_saved_plan_and_default_off(tmp_path, monkeypatch):
    runner = CommandRunner()
    execute = Mock(return_value=b"")
    monkeypatch.setattr(runner, "_execute", execute)
    with pytest.raises(PlanError, match="APPLY_DISABLED"):
        runner.apply_saved(tmp_path, {}, lambda: False)
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    runner.apply_saved(tmp_path, {}, lambda: False)
    assert execute.call_args.args[0] == ["apply", "-input=false", "-no-color", "-lock-timeout=30s", "saved.tfplan"]
    with pytest.raises(PlanError, match="COMMAND_FORBIDDEN"):
        runner.run(["apply", "other.tfplan"], tmp_path, {}, lambda: False)


@pytest.mark.parametrize("change", ["foreign", "missing", "protected", "disk", "node", "power"])
def test_provider_observations_are_strictly_scoped(change):
    allocation = Allocation.model_validate(allocation_data())
    variables = allocation.variables(SimpleNamespace(**SPEC))
    actual = plan_json(variables)["resource_changes"][0]["change"]["after"]
    actual.update(started=False, id=str(actual["vm_id"]))
    resource = {"address": ADDRESS, "values": actual}
    if change == "foreign": resource["address"] = "another.vm"
    if change == "protected": actual["vm_id"] = 104
    if change == "disk": actual["disk"][0]["size"] = 10
    if change == "node": actual["node_name"] = "other-node"
    if change == "power": actual["started"] = None
    with pytest.raises(PlanError):
        observed_resource({"root_module": {"resources": [] if change == "missing" else [resource]}}, variables)


def test_adapter_rechecks_plan_before_fixed_apply_and_observes_provider(tmp_path, monkeypatch):
    from tests.test_historical_recovery import artifacts
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    allocation = Allocation.model_validate(allocation_data())
    allocation.started = True
    variables = allocation.variables(SimpleNamespace(**SPEC))
    commands = []
    planned = plan_json(variables)
    actual = planned["resource_changes"][0]["change"]["after"]
    actual.update(started=True, id=str(actual["vm_id"]))
    class Runner:
        def run(self, args, cwd, env, cancelled, allowed=(0,), phase=None):
            commands.append(args)
            if args[0] == "version":
                from iacp.terraform.config import TERRAFORM_VERSION
                return json.dumps({"terraform_version": TERRAFORM_VERSION}).encode()
            if args == ["show", "-json", "saved.tfplan"]: return json.dumps(planned).encode()
            if args == ["state", "pull"]:
                return json.dumps({"version": 4, "lineage": "fixture-lineage", "serial": 3, "resources": [{
                    "module": "module.kvm", "mode": "managed", "type": "proxmox_virtual_environment_vm", "name": "this",
                    "instances": [{"attributes": actual}]}]}).encode()
            if args == ["show", "-json", "observation.tfplan"]:
                return json.dumps({"complete": True, "errored": False, "planned_values": {},
                    "prior_state": {"values": {"root_module": {"child_modules": [{"resources": [
                        {"address": ADDRESS, "values": actual}]}]}}}}).encode()
            return b""
        def apply_saved(self, root, env, cancelled):
            assert commands[-1] == ["authorized"]
            commands.append(["apply", "saved.tfplan"])
    executor, _, _, _, context = artifacts(tmp_path, monkeypatch, allocation=allocation, variables=variables)
    executor.runner = Runner()
    integrity = Mock()
    monkeypatch.setattr(executor, "prepare_apply", integrity)
    monkeypatch.setattr(executor, "environment", lambda workspace: {})
    monkeypatch.setattr("iacp.terraform.executor.proxmox_resource", lambda *args: {"present": True, "power_state": "running"})
    result = executor.apply(*context,
                            lambda: False, lambda: commands.append(["authorized"]))
    assert integrity.call_count == 2 and result["power_state"] == "running"
    assert [row for row in commands if row[0] == "apply"] == [["apply", "saved.tfplan"]]
    assert [row for row in commands if row[0] == "plan"][0][1] == "-refresh-only"


def test_broker_uncertainty_recovery_fences_late_apply(client, auth, prepared, application, monkeypatch, sessions):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    publisher = Mock(side_effect=TimeoutError("private broker endpoint"))
    application.dependency_overrides[get_apply_publisher] = lambda: publisher
    row = approval(client, prepared).json()
    assert dispatch(client, prepared, auth, row).status_code == 503
    replay = dispatch(client, prepared, auth, row)
    assert replay.status_code == 202 and replay.json()["status"] == "queued" and publisher.call_count == 1
    publisher.side_effect = None
    response = client.post(prepared[2] + "/applies/" + row["approval_id"] + "/recover", headers=auth)
    assert response.status_code == 202 and publisher.call_args.args == (row["approval_id"], "reconcile")
    assert response.json()["status"] == "uncertain"
    fake = ApplyingExecutor()
    execute_apply(row["approval_id"], sessions, fake)
    execute_apply(row["approval_id"], sessions, fake, reconcile=True)
    execute_apply(row["approval_id"], sessions, fake)
    assert not fake.calls


def test_worker_rechecks_expiry_after_queue_wait(client, auth, prepared, sessions, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared).json()
    dispatch(client, prepared, auth, row)
    with sessions() as session:
        session.get(TerraformApply, row["approval_id"]).expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    fake = ApplyingExecutor()
    execute_apply(row["approval_id"], sessions, fake)
    assert not fake.calls


def test_authorized_lab_validation_never_creates_executor(tmp_path, monkeypatch, capsys):
    from iacp.terraform import lab
    request, allocation = tmp_path / "request.json", tmp_path / "allocation.json"
    request.write_text(json.dumps(SPEC))
    allocation.write_text(json.dumps(allocation_data()))
    monkeypatch.setattr("sys.argv", ["lab", "--request", str(request), "--allocation", str(allocation)])
    monkeypatch.setattr("subprocess.Popen", Mock(side_effect=AssertionError("Network/executor forbidden")))
    lab.main()
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] and result["network_contacted"] is result["apply_enabled"] is False


def test_independent_subject_required_even_with_approver_role(client, auth, prepared, monkeypatch):
    from pathlib import Path
    path = Path(os.environ["IACP_TENANT_CREDENTIALS_FILE"])
    registry = json.loads(path.read_text())
    registry[0]["subject"] = "bootstrap"
    path.write_text(json.dumps(registry))
    assert approval(client, prepared).json()["detail"] == "INDEPENDENT_APPROVAL_REQUIRED"


def test_changed_plan_after_dispatch_never_reaches_apply(client, auth, prepared, sessions, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared).json()
    dispatch(client, prepared, auth, row)
    with sessions() as session:
        session.get(TerraformExecution, prepared[0]["instance_id"]).plan_sha256 = "b" * 64
        session.commit()
    fake = ApplyingExecutor()
    execute_apply(row["approval_id"], sessions, fake)
    assert not fake.calls
    assert client.get(prepared[2] + "/applies", headers=auth).json()[0]["error_code"] == "PLAN_CHANGED"


def test_runner_limits_output_without_leaking_diagnostics(tmp_path):
    import sys
    runner = CommandRunner((sys.executable, "-c", "import sys; sys.stderr.write('private-' * 2500000)"))
    with pytest.raises(PlanError, match="OUTPUT_LIMIT_EXCEEDED") as failure:
        runner.run(["show"], tmp_path, {}, lambda: False)
    assert "private" not in str(failure.value)


def test_cancel_before_mutation_keeps_operation_consistent(client, auth, prepared):
    row = approval(client, prepared).json()
    cancelled = client.post(prepared[2] + "/applies/" + row["approval_id"] + "/cancel", headers=auth)
    assert cancelled.json()["status"] == "cancelled"
    operation = client.get('/v1/operations/' + prepared[0]["operation_id"], headers=auth).json()
    assert operation["status"] == "failed" and operation["error_code"] == "CANCELLED"


def test_failed_command_exposes_only_safe_structured_diagnostic(tmp_path):
    import sys
    runner = CommandRunner((sys.executable, "-c", "import sys; sys.stderr.write('secret'); sys.exit(7)"))
    with pytest.raises(PlanError) as raised:
        runner.run(["init"], tmp_path, {"PATH": os.environ["PATH"]}, lambda: False, phase="apply.init")
    assert raised.value.code == "TERRAFORM_COMMAND_FAILED"
    assert raised.value.diagnostic() == {"phase": "apply.init", "command": "init", "exit_code": 7}
    assert "secret" not in str(raised.value.diagnostic())


def test_apply_persists_failure_phase_without_stderr(client, auth, prepared, monkeypatch, sessions):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared).json()
    dispatch(client, prepared, auth, row)
    failure = PlanError("TERRAFORM_COMMAND_FAILED", phase="apply", command="apply", exit_code=1)
    execute_apply(row["approval_id"], sessions, ApplyingExecutor(failure=failure))
    current = client.get(prepared[2] + "/applies", headers=auth).json()[0]
    assert current["status"] == "uncertain"
    assert current["result"] == {"diagnostic": {"phase": "apply", "command": "apply", "exit_code": 1}}


def test_preflight_failure_is_failed_not_uncertain(client, auth, prepared, monkeypatch, sessions):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared).json()
    dispatch(client, prepared, auth, row)
    failure = PlanError("TERRAFORM_COMMAND_FAILED", phase="apply.init", command="init", exit_code=1)
    execute_apply(row["approval_id"], sessions, ApplyingExecutor(failure=failure, before_failure=True))
    current = client.get(prepared[2] + "/applies", headers=auth).json()[0]
    assert current["status"] == "failed" and current["started_at"] is None
    assert current["result"]["diagnostic"]["phase"] == "apply.init"
