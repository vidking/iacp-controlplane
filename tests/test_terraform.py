import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from iacp.api.dependencies import get_plan_publisher
from iacp.models import Instance, Job, Operation, TerraformAttempt, TerraformExecution
from iacp.terraform.config import Allocation, Cancelled, PlanError, SOURCE_REVISION, TERRAFORM_VERSION
from iacp.terraform.executor import ADDRESS, PlanResult, TerraformExecutor, verify_plan
from iacp.terraform.lifecycle import execute_plan
from iacp.terraform.locks import instance_lock
from iacp.terraform.runner import CommandRunner
from tests.test_instances import SPEC, create, identities


def allocation_data(vmid=21001, image="test-image"):
    # Fixtures sintéticos; no autorizan recursos reales ni se usan para acceder a red.
    return {"image": image, "environment": "lab", "allowed_nodes": ["mng01", "mng02"],
            "backend": {"bucket": "fixture-iacp-lab", "region": "us-east-1", "endpoint": "https://s3.wasabisys.com"},
            "vm_id": vmid, "template_vm_id": 9000, "node_name": "mng01", "network_bridge": "fixture-bridge",
            "vlan_id": 204, "ipv4_cidr": "192.0.2.10/24", "ipv4_gateway": "192.0.2.1",
            "cloudinit_datastore_id": "fixture-storage", "cloudinit_username": "fixture-user",
            "ssh_public_keys": ["ssh-ed25519 AAAATESTKEYONLY"],
            "boot_disk": {"interface": "scsi0", "datastore_id": "fixture-storage", "minimum_size_gb": 20}}


class FakeExecutor:
    def __init__(self, fail=None, callback=None):
        self.calls, self.fail, self.callback = [], fail, callback

    def plan(self, instance_id, number, state_key, allocation, variables, cancelled):
        self.calls.append((instance_id, number, state_key, allocation.model_dump(), variables))
        if self.callback:
            self.callback()
        if cancelled():
            raise Cancelled()
        if self.fail:
            raise self.fail
        return PlanResult("a" * 64, SOURCE_REVISION, {"apply_enabled": False, "reserved_vmid": allocation.vm_id})

    def prepare_apply(self, *args):
        return {"approval_required": True, "execution_enabled": False}


@pytest.fixture
def terraform_mode(monkeypatch):
    monkeypatch.setenv("IACP_INSTANCE_EXECUTOR", "terraform")


def run_plan(accepted, sessions, executor=None, load=None, attempt=1):
    executor = executor or FakeExecutor()
    execute_plan(accepted["job_id"], attempt, sessions, executor, load or (lambda instance: Allocation.model_validate(allocation_data())))
    return executor


def execution(client, auth, accepted):
    return client.get(f'/v1/instances/{accepted["instance_id"]}/provisioning/plan', headers=auth)


def test_plan_is_persistent_and_waits_for_approval(client, auth, terraform_mode, sessions):
    accepted = create(client, auth).json()
    assert accepted["executor"] == "provisioning"
    executor = run_plan(accepted, sessions)
    result = execution(client, auth, accepted).json()
    assert result["status"] == "planned" and result["attempt"] == 1
    assert result["apply_enabled"] is False and result["approval_required"] is True
    assert result["source_revision"] == SOURCE_REVISION
    assert accepted["instance_id"] in result["state_key"]
    row = client.get(f'/v1/instances/{accepted["instance_id"]}', headers=auth).json()
    assert row["status"] == "pending" and row["vmid"] is row["node"] is None
    assert row["result"]["phase"] == "awaiting_approval"
    assert client.get(f'/v1/jobs/{accepted["job_id"]}', headers=auth).json()["status"] == "completed"
    assert client.get(f'/v1/operations/{accepted["operation_id"]}', headers=auth).json()["status"] == "running"
    run_plan(accepted, sessions, executor)
    assert len(executor.calls) == 1
    assert execution(client, auth, accepted).json() == result


def test_real_plan_is_disabled_without_credentials_or_files(client, auth, terraform_mode, sessions, monkeypatch):
    monkeypatch.delenv("IACP_TERRAFORM_PLAN_ENABLED", raising=False)
    accepted = create(client, auth).json()
    execute_plan(accepted["job_id"], 1, sessions)
    result = execution(client, auth, accepted).json()
    assert result["status"] == "failed" and result["error_code"] == "REAL_PLAN_DISABLED"


def test_cancel_before_delivery_and_idempotent_retry_keep_identifiers(client, auth, terraform_mode, sessions, application):
    accepted = create(client, auth).json()
    path = f'/v1/instances/{accepted["instance_id"]}/provisioning/plan'
    cancelled = client.post(path + "/cancel", headers=auth).json()
    assert cancelled["status"] == "cancelled"
    assert client.post(path + "/cancel", headers=auth).json() == cancelled
    executor = run_plan(accepted, sessions)
    assert not executor.calls
    published = Mock()
    application.dependency_overrides[get_plan_publisher] = lambda: published
    headers = {**auth, "Idempotency-Key": "retry-001"}
    response = client.post(path + "/retry", headers=headers, json={"expected_attempt": 1})
    assert response.status_code == 202 and response.json()["attempt"] == 2
    assert response.json()["job_id"] == accepted["job_id"]
    assert client.post(path + "/retry", headers=headers, json={"expected_attempt": 1}).json() == response.json()
    published.assert_called_once_with(accepted["job_id"], 2)
    run_plan(accepted, sessions, executor, attempt=1)  # mensaje viejo no reclama la generación nueva
    assert not executor.calls
    run_plan(accepted, sessions, executor, attempt=2)
    assert len(executor.calls) == 1
    assert client.post(path + "/retry", headers={**auth, "Idempotency-Key": "other"}, json={"expected_attempt": 1}).status_code == 409
    with sessions() as session:
        for model in (Instance, Operation, Job):
            assert session.scalar(select(func.count()).select_from(model)) == 1
        assert session.scalar(select(func.count()).select_from(TerraformAttempt)) == 2


def test_cancel_running_plan_and_reject_retry_while_worker_holds_lock(client, auth, terraform_mode, sessions):
    accepted = create(client, auth).json()
    path = f'/v1/instances/{accepted["instance_id"]}/provisioning/plan'

    def callback():
        retry = client.post(path + "/retry", headers={**auth, "Idempotency-Key": "active"}, json={"expected_attempt": 1})
        assert retry.status_code == 409
        assert client.post(path + "/cancel", headers=auth).json()["cancel_requested"]

    run_plan(accepted, sessions, FakeExecutor(callback=callback))
    result = execution(client, auth, accepted).json()
    assert result["status"] == "cancelled" and result["plan_sha256"] is None


def test_failed_plan_retries_with_frozen_allocation_and_same_state(client, auth, terraform_mode, sessions):
    accepted = create(client, auth).json()
    original = run_plan(accepted, sessions, FakeExecutor(fail=RuntimeError("secret credential")))
    failed = execution(client, auth, accepted).json()
    assert failed["error_code"] == "TERRAFORM_EXECUTION_FAILED"
    assert "secret" not in json.dumps(failed)
    path = f'/v1/instances/{accepted["instance_id"]}/provisioning/plan/retry'
    assert client.post(path, headers={**auth, "Idempotency-Key": "recovery"}, json={"expected_attempt": 1}).status_code == 202
    loader = Mock(side_effect=AssertionError("A retry must not allocate another VM"))
    retried = run_plan(accepted, sessions, load=loader, attempt=2)
    loader.assert_not_called()
    assert retried.calls[0][2:4] == original.calls[0][2:4]


def test_recover_interrupted_attempt_after_lock_release(client, auth, terraform_mode, sessions):
    accepted = create(client, auth).json()
    with sessions() as session:
        row = session.get(TerraformExecution, accepted["instance_id"])
        row.status = "running"
        session.get(Job, accepted["job_id"]).status = "running"
        attempt = session.scalar(select(TerraformAttempt))
        attempt.status = "running"
        session.commit()
    path = f'/v1/instances/{accepted["instance_id"]}/provisioning/plan/retry'
    result = client.post(path, headers={**auth, "Idempotency-Key": "recover-001"}, json={"expected_attempt": 1})
    assert result.status_code == 202
    assert execution(client, auth, accepted).json()["attempts"][0]["status"] == "interrupted"
    run_plan(accepted, sessions, attempt=2)
    assert execution(client, auth, accepted).json()["status"] == "planned"


def test_vmid_reservation_prevents_other_instance_using_same_target(client, auth, terraform_mode, sessions):
    first = create(client, auth).json()
    run_plan(first, sessions)
    second = create(client, auth, key="second", spec={**SPEC, "name": "other-kvm"}).json()
    fake = run_plan(second, sessions)
    assert not fake.calls
    assert execution(client, auth, second).json()["error_code"] == "VMID_ALREADY_RESERVED"


def test_plan_actions_are_tenant_scoped_and_reader_cannot_mutate(client, auth, identities, terraform_mode):
    operator = identities["tenant-a-operator"]["headers"]
    reader = identities["tenant-a-reader"]["headers"]
    other = identities["tenant-b-operator"]["headers"]
    accepted = create(client, operator).json()
    path = f'/v1/instances/{accepted["instance_id"]}/provisioning/plan'
    assert client.get(path, headers=reader).status_code == 403
    for endpoint in (path, path + "/cancel", path + "/retry"):
        method = client.get if endpoint == path else client.post
        assert method(endpoint, headers={**other, "Idempotency-Key": "other"}, **({} if endpoint == path else {"json": {"expected_attempt": 1}})).status_code == 403
    assert client.post(path + "/cancel", headers=reader).status_code == 403
    assert client.post(path + "/retry", headers=reader, json={"expected_attempt": 1}).status_code == 403


@pytest.mark.parametrize("change", [{"vm_id": 104}, {"template_vm_id": 104}, {"node_name": "other"},
                                  {"ipv4_cidr": "not-ipam"}, {"boot_disk": {"interface": "../disk", "datastore_id": "fixture", "minimum_size_gb": 20}}])
def test_invalid_allocations_are_rejected(change):
    with pytest.raises(ValueError):
        Allocation.model_validate({**allocation_data(), **change})


def plan_json(variables):
    row = variables["instance"]
    return {"resource_changes": [{"address": ADDRESS, "change": {"actions": ["create"], "after": {
        "vm_id": row["vm_id"], "node_name": row["node_name"], "name": row["vm_name"],
        "cpu": [{"cores": row["cpu_cores"]}], "memory": [{"dedicated": row["memory_mb"]}],
        "clone": [{"vm_id": row["template_vm_id"]}],
        "disk": [{"interface": row["boot_disk"]["interface"], "size": row["boot_disk"]["size_gb"],
                  "datastore_id": row["boot_disk"]["datastore_id"],
                  **{key: row["boot_disk"][key] for key in ("aio", "cache", "discard", "backup", "replicate", "iothread", "ssd", "file_format")}}],
        "started": row["started"], "pool_id": row["pool_id"], "on_boot": False,
        "network_device": [{"bridge": row["network_bridge"], "model": "virtio", "vlan_id": row["vlan_id"]}],
        "agent": [{"enabled": row["guest_agent_enabled"]}],
        "initialization": [{"datastore_id": row["cloudinit_datastore_id"],
                            "ip_config": [{"ipv4": [{"address": row["ipv4_cidr"], "gateway": row["ipv4_gateway"]}]}],
                            "user_account": [{"username": row["cloudinit_username"], "keys": row["ssh_public_keys"]}]}],
    }}}]}


@pytest.mark.parametrize("kind", ["destroy", "replace", "wrong-vmid", "foreign-state", "disk-mismatch"])
def test_plan_policy_rejects_destructive_or_foreign_resources(kind):
    allocation = Allocation.model_validate(allocation_data())
    variables = allocation.variables(SimpleNamespace(**SPEC))
    plan = plan_json(variables)
    change = plan["resource_changes"][0]["change"]
    if kind in {"destroy", "replace"}:
        change["actions"] = ["delete"] if kind == "destroy" else ["delete", "create"]
    elif kind == "wrong-vmid":
        change["after"]["vm_id"] = 104
    elif kind == "disk-mismatch":
        change["after"]["disk"][0]["size"] = 10
    else:
        plan["prior_state"] = {"values": {"root_module": {"resources": [{"address": "controlplane", "values": {"vm_id": 104}}]}}}
    with pytest.raises(PlanError):
        verify_plan(plan, variables)


def test_real_adapter_builds_plan_from_pinned_archive_and_preserves_integrity(tmp_path, monkeypatch):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        item = tarfile.TarInfo("stacks/kvm-instance/versions.tf")
        content = b"# Fixture without Terraform resources\n"
        item.size = len(content)
        tar.addfile(item, io.BytesIO(content))
    git = Mock(return_value=SimpleNamespace(stdout=archive.getvalue()))
    monkeypatch.setattr(subprocess, "run", git)
    monkeypatch.setenv("IACP_TERRAFORM_PLAN_ENABLED", "1")
    monkeypatch.setenv("TF_CLI_ARGS", "-destroy -lock=false")
    credentials = tmp_path / "credentials"
    credentials.write_text(json.dumps({"AWS_ACCESS_KEY_ID": "fixture", "AWS_SECRET_ACCESS_KEY": "fixture-secret",
                                      "PROXMOX_VE_API_TOKEN": "fixture-secret", "PROXMOX_VE_ENDPOINT": "https://fixture.invalid:8006/"}))
    monkeypatch.setenv("IACP_TERRAFORM_CREDENTIALS_FILE", str(credentials))
    allocation = Allocation.model_validate(allocation_data())
    variables = allocation.variables(SimpleNamespace(**SPEC))

    class Runner:
        def __init__(self): self.commands = []
        def run(self, args, cwd, env, cancelled, allowed=(0,), phase=None):
            self.commands.append(args)
            assert "TF_CLI_ARGS" not in env
            if args[0] == "version": return json.dumps({"terraform_version": TERRAFORM_VERSION}).encode()
            if args[0] == "plan": (cwd / "saved.tfplan").write_bytes(b"fixture-plan")
            if args[0] == "show": return json.dumps(plan_json(variables)).encode()
            return b""

    runner = Runner()
    executor = TerraformExecutor(tmp_path / "checkout", tmp_path / "plans", runner)
    from uuid import uuid4
    identifier = str(uuid4())
    result = executor.plan(identifier, 1, "iacp/lab/fixture/terraform.tfstate", allocation, variables, lambda: False)
    assert git.call_args.args[0][-1] == SOURCE_REVISION
    assert [command[0] for command in runner.commands] == ["version", "init", "validate", "plan", "show"]
    workspace = executor.workspace(identifier, 1)
    assert (workspace.stat().st_mode & 0o777) == 0o700
    assert (workspace / "manifest.json").stat().st_mode & 0o777 == 0o600
    preparation = executor.prepare_apply(identifier, 1, result.sha256, "iacp/lab/fixture/terraform.tfstate", allocation.model_dump(), variables)
    assert preparation["execution_enabled"] is False
    (workspace / "source/stacks/kvm-instance/saved.tfplan").write_bytes(b"tampered")
    with pytest.raises(PlanError, match="PLAN_INTEGRITY_FAILED"):
        executor.prepare_apply(identifier, 1, result.sha256, "iacp/lab/fixture/terraform.tfstate", allocation.model_dump(), variables)
    with pytest.raises(PlanError, match="APPLY_DISABLED"):
        executor.apply()


def test_apply_rejects_changed_execution_credentials_without_persisting_them(tmp_path, monkeypatch):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        item = tarfile.TarInfo("stacks/kvm-instance/versions.tf")
        item.size = 0
        tar.addfile(item, io.BytesIO(b""))
    monkeypatch.setattr(subprocess, "run", Mock(return_value=SimpleNamespace(stdout=archive.getvalue())))
    monkeypatch.setenv("IACP_TERRAFORM_PLAN_ENABLED", "1")
    credentials = tmp_path / "credentials"
    original = {"AWS_ACCESS_KEY_ID": "fixture", "AWS_SECRET_ACCESS_KEY": "first-secret",
                "PROXMOX_VE_API_TOKEN": "first-token", "PROXMOX_VE_ENDPOINT": "https://fixture.invalid:8006/"}
    credentials.write_text(json.dumps(original))
    monkeypatch.setenv("IACP_TERRAFORM_CREDENTIALS_FILE", str(credentials))
    allocation = Allocation.model_validate(allocation_data())
    variables = allocation.variables(SimpleNamespace(**SPEC))

    class Runner:
        def run(self, args, cwd, env, cancelled, allowed=(0,), phase=None):
            if args[0] == "version": return json.dumps({"terraform_version": TERRAFORM_VERSION}).encode()
            if args[0] == "plan": (cwd / "saved.tfplan").write_bytes(b"fixture-plan")
            if args[0] == "show": return json.dumps(plan_json(variables)).encode()
            return b""

    executor = TerraformExecutor(tmp_path / "checkout", tmp_path / "plans", Runner())
    from uuid import uuid4
    identifier = str(uuid4())
    result = executor.plan(identifier, 1, "iacp/lab/fixture/terraform.tfstate", allocation, variables, lambda: False)
    manifest = (executor.workspace(identifier, 1) / "manifest.json").read_text()
    assert "first-secret" not in manifest and "first-token" not in manifest
    credentials.write_text(json.dumps({**original, "PROXMOX_VE_API_TOKEN": "second-token"}))
    with pytest.raises(PlanError, match="PLAN_INTEGRITY_FAILED"):
        executor.prepare_apply(identifier, 1, result.sha256, "iacp/lab/fixture/terraform.tfstate",
                               allocation.model_dump(), variables)


@pytest.mark.parametrize("command", [["apply"], ["destroy"], ["state", "push"], ["plan", "-lock=false"], ["plan", "-destroy"]])
def test_runner_never_executes_mutating_commands(tmp_path, command):
    with pytest.raises(PlanError, match="COMMAND_FORBIDDEN"):
        CommandRunner().run(command, tmp_path, {}, lambda: False)


@pytest.mark.parametrize("action", ["timeout", "cancel"])
def test_runner_terminates_process_on_timeout_or_cancellation(tmp_path, action):
    calls = []
    def cancelled():
        calls.append(1)
        return action == "cancel" and len(calls) > 2
    runner = CommandRunner((sys.executable, "-c", "import time; time.sleep(20)"), timeout=0.2)
    start = time.monotonic()
    with pytest.raises(PlanError, match="CANCELLED" if action == "cancel" else "TERRAFORM_TIMEOUT"):
        runner.run(["plan"], tmp_path, {"PATH": os.environ["PATH"]}, cancelled)
    assert time.monotonic() - start < 5


def test_runner_keeps_stderr_separate_from_structured_output(tmp_path):
    runner = CommandRunner((sys.executable, "-c", "import sys; print('{}'); print('diagnostic', file=sys.stderr)"))
    assert json.loads(runner.run(["show"], tmp_path, {}, lambda: False)) == {}


def test_retry_dispatch_failure_can_be_recovered_without_reallocating(client, auth, terraform_mode, sessions, application):
    accepted = create(client, auth).json()
    run_plan(accepted, sessions, FakeExecutor(fail=PlanError("TERRAFORM_COMMAND_FAILED")))
    path = f'/v1/instances/{accepted["instance_id"]}/provisioning/plan/retry'
    published = Mock(side_effect=TimeoutError("sensitive broker connection"))
    application.dependency_overrides[get_plan_publisher] = lambda: published
    headers = {**auth, "Idempotency-Key": "failed-publication"}
    assert client.post(path, headers=headers, json={"expected_attempt": 1}).status_code == 503
    assert client.post(path, headers=headers, json={"expected_attempt": 1}).status_code == 202
    published.assert_called_once()
    state = execution(client, auth, accepted).json()
    assert state["attempt"] == 2 and state["error_code"] == "DISPATCH_FAILED"
    assert "sensitive" not in json.dumps(state)


def test_cancel_prepared_plan_invalidates_current_hash(client, auth, terraform_mode, sessions):
    accepted = create(client, auth).json()
    run_plan(accepted, sessions)
    path = f'/v1/instances/{accepted["instance_id"]}/provisioning/plan'
    response = client.post(path + "/cancel", headers=auth).json()
    assert response["status"] == "cancelled" and response["plan_sha256"] is None
    assert client.get(f'/v1/instances/{accepted["instance_id"]}', headers=auth).json()["result"] is None


def test_wasabi_backend_cannot_disable_locking_or_encryption():
    backend = Allocation.model_validate(allocation_data()).backend.arguments("iacp/lab/fixture/terraform.tfstate")
    assert backend["use_lockfile"] is backend["encrypt"] is backend["use_path_style"] is True
    assert backend["key"] == "iacp/lab/fixture/terraform.tfstate"
    assert not {"access_key", "secret_key", "profile"} & backend.keys()


def test_recover_unpublished_queued_attempt_fences_late_delivery(client, auth, terraform_mode, sessions):
    accepted = create(client, auth).json()
    path = f'/v1/instances/{accepted["instance_id"]}/provisioning/plan/retry'
    response = client.post(path, headers={**auth, "Idempotency-Key": "recover-unpublished"}, json={"expected_attempt": 1})
    assert response.status_code == 202 and response.json()["attempt"] == 2
    fake = run_plan(accepted, sessions, attempt=1)
    assert not fake.calls
    run_plan(accepted, sessions, fake, attempt=2)
    assert len(fake.calls) == 1
    assert execution(client, auth, accepted).json()["attempts"][0]["status"] == "interrupted"
