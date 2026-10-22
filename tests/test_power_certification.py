"""Synthetic historical VM 106; no real services or VM credentials."""
from copy import deepcopy
import hashlib
import io
import json
import tarfile
from unittest.mock import Mock

import pytest
from sqlalchemy import select, text

from iacp.lifecycle import certify as certification, ownership
from iacp.lifecycle.execution import execute_power
from iacp.models import (BOOTSTRAP_TENANT_ID, Instance, Operation, OperationEvent,
                         PowerOwnershipCertificate, TerraformApply, TerraformExecution)
from iacp.terraform.config import Allocation, PlanError
from iacp.terraform.executor import ADDRESS, write_private
from iacp.terraform.state import StateSnapshot
from tests.power_helpers import FakePower, managed_instance
from tests.test_historical_recovery import artifacts
from tests.test_power import power, request
from tests.test_terraform import allocation_data, plan_json


@pytest.fixture
def historical(sessions, tmp_path, monkeypatch):
    identifier = managed_instance(sessions, BOOTSTRAP_TENANT_ID, vmid=106, name="fixture-106")
    allocation = Allocation.model_validate(allocation_data(vmid=106, image="synthetic-image"))
    with sessions() as session:
        instance = session.get(Instance, identifier)
        variables = allocation.variables(instance)
    executor, runner, workspace, root, args = artifacts(tmp_path, monkeypatch,
        identifier=identifier, attempt=4, allocation=allocation, variables=variables)
    manifest = json.loads((workspace / "manifest.json").read_text())
    manifest["source_revision"] = ownership.LEGACY_TERRAFORM_POWER_SOURCE
    write_private(workspace / "manifest.json", manifest)
    with sessions() as session:
        execution = session.get(TerraformExecution, identifier)
        approval = session.scalar(select(TerraformApply).where(TerraformApply.instance_id == identifier))
        execution.source_revision = ownership.LEGACY_TERRAFORM_POWER_SOURCE
        execution.attempt, execution.plan_sha256 = 4, args[2]
        execution.state_key, execution.allocation = args[3], allocation.model_dump()
        approval.attempt, approval.plan_sha256 = 4, args[2]
        session.commit()
        approval_id = approval.id
    native = FakePower()
    native._request = Mock(return_value={"version": "fixture-pve"})
    monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "0")
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    monkeypatch.setattr(certification, "S3StateReader", lambda backend, env: runner)
    planner = Mock(return_value={"actions": ["no-op"], "plan_sha256": "c" * 64, "backend": "isolated-local"})
    monkeypatch.setattr(certification, "offline_plan", planner)
    expected = certification.CertificationRequest(instance_id=identifier, tenant_id=BOOTSTRAP_TENANT_ID,
        approval_id=approval_id, attempt=4, vmid=106, node="mng01", name="fixture-106",
        source_revision=ownership.LEGACY_TERRAFORM_POWER_SOURCE, plan_sha256=args[2], state_key=args[3],
        state=runner.read(lambda: False).metadata(), pve_version="fixture-pve", actor="fixture-reviewer",
        change_reference="fixture-change", release_reference="fixture-release@sha256:" + "d" * 64)
    return expected, executor, runner, native, planner


def history(sessions):
    with sessions() as session:
        return {name: session.execute(text(f'SELECT * FROM "{name}"')).all() for name in (
            "instances", "terraform_executions", "terraform_attempts", "terraform_applies", "operations", "jobs")}


def test_pins_and_flags_cannot_promote_a_historical_instance(client, auth, sessions, power):
    identifier, native, publisher = power
    with sessions() as session:
        session.get(TerraformExecution, identifier).source_revision = ownership.LEGACY_TERRAFORM_POWER_SOURCE
        session.commit()
    assert request(client, auth, identifier).json()["detail"] == "POWER_OWNERSHIP_CERTIFICATION_REQUIRED"
    publisher.assert_not_called()
    assert not native.calls
    assert client.get(f'/v1/instances/{identifier}/power', headers=auth).json()["blocked_reason"] == "POWER_OWNERSHIP_CERTIFICATION_REQUIRED"


def test_validation_is_readonly_and_recording_is_audited_without_rewriting_history(historical, sessions):
    expected, executor, runner, native, planner = historical
    before = history(sessions)
    paths = [path for path in executor.workspace(expected.instance_id, 4).rglob('*') if path.is_file()]
    contents = {str(path): path.read_bytes() for path in paths}
    result = certification.certify(expected, sessions, executor, client=native)
    assert result["validated"] and not result["recorded"]
    with sessions() as session:
        assert session.scalar(select(PowerOwnershipCertificate)) is None
    assert history(sessions) == before
    result = certification.certify(expected, sessions, executor, record=True, client=native)
    assert result["certificate_id"]
    assert history(sessions) == before
    assert contents == {str(path): path.read_bytes() for path in paths}
    with sessions() as session:
        certificate = session.get(PowerOwnershipCertificate, result["certificate_id"])
        assert certificate.source_revision == ownership.LEGACY_TERRAFORM_POWER_SOURCE
        assert certificate.certified_by == expected.actor and certificate.change_reference == expected.change_reference
        assert certificate.evidence["state_consistency"]["change"] == "unchanged"
        event = session.scalar(select(OperationEvent).where(OperationEvent.code == "power_ownership_certified"))
        assert event.actor == expected.actor
    assert certification.certify(expected, sessions, executor, record=True, client=native)["certificate_id"] == result["certificate_id"]
    with sessions() as session:
        assert len(session.scalars(select(OperationEvent).where(OperationEvent.code == "power_ownership_certified")).all()) == 1
    assert not any(call[0] == "submit" for call in native.calls)
    runner.apply_saved.assert_not_called()
    # Normal prepare/apply still rejects this valid old binary plan.
    with sessions() as session:
        allocation = session.get(TerraformExecution, expected.instance_id).allocation
    with pytest.raises(PlanError, match="PLAN_INTEGRITY_FAILED"):
        executor.prepare_apply(expected.instance_id, 4, expected.plan_sha256, expected.state_key,
                               allocation, runner.variables)


@pytest.mark.parametrize("field,value", [
    ("vmid", 104), ("tenant_id", "f" * 36), ("name", "another-vm"), ("node", "mng02"),
    ("approval_id", "f" * 36), ("attempt", 3), ("plan_sha256", "f" * 64),
    ("state_key", "another-key"), ("source_revision", "f" * 40),
])
def test_certification_rejects_expected_identity_mismatches(historical, sessions, field, value):
    expected, executor, runner, native, planner = historical
    altered = expected.model_copy(update={field: value})
    with pytest.raises(PlanError, match="POWER_CERTIFICATION_IDENTITY_MISMATCH"):
        certification.certify(altered, sessions, executor, record=True, client=native)
    with sessions() as session:
        assert session.scalar(select(PowerOwnershipCertificate)) is None
    planner.assert_not_called()


@pytest.mark.parametrize("change", ["tampered-plan", "state-version", "state-lineage", "state-serial", "external-write", "live-power", "pve-version", "enabled", "apply-disabled"])
def test_failed_checks_never_issue_certificate(historical, sessions, monkeypatch, change):
    expected, executor, runner, native, planner = historical
    if change == "tampered-plan":
        (executor.workspace(expected.instance_id, 4) / "source/stacks/kvm-instance/saved.tfplan").write_bytes(b"tampered")
    elif change.startswith("state-"):
        key = {"state-version": "version_id", "state-lineage": "lineage", "state-serial": "serial"}[change]
        expected = expected.model_copy(update={"state": expected.state.model_copy(update={key: 100 if key == "serial" else "changed"})})
    elif change == "external-write":
        original = runner.read
        count = 0
        def read(cancelled):
            nonlocal count
            count += 1
            return original(cancelled) if count == 1 else StateSnapshot.parse(json.dumps(runner.state()).encode(), "new-version")
        monkeypatch.setattr(runner, "read", read)
    elif change == "live-power": native.power = "running"
    elif change == "pve-version": native._request.return_value = {"version": "different"}
    elif change == "enabled": monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "1")
    elif change == "apply-disabled": monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "0")
    before = history(sessions)
    with pytest.raises(PlanError):
        certification.certify(expected, sessions, executor, record=True, client=native)
    assert history(sessions) == before
    with sessions() as session:
        assert session.scalar(select(PowerOwnershipCertificate)) is None
        assert session.scalar(select(OperationEvent)) is None
    assert not any(call[0] == "submit" for call in native.calls)


def noop(variables, values):
    plan = plan_json(variables)
    plan.update(errored=False, complete=True, applyable=False)
    plan["resource_changes"][0]["change"] = {"actions": ["no-op"], "before": deepcopy(values), "after": deepcopy(values), "after_unknown": {}}
    module = {"root_module": {"resources": [{"address": ADDRESS, "values": deepcopy(values)}]}}
    plan["prior_state"] = {"values": deepcopy(module)}
    plan["planned_values"] = deepcopy(module)
    return plan


@pytest.mark.parametrize("change", [None, "create", "update", "delete", "replace", "unknown", "incomplete", "drift", "checks", "foreign", "power"])
def test_non_destructive_plan_policy(historical, change):
    expected, executor, runner, native, planner = historical
    plan = noop(runner.variables, runner.values())
    row = plan["resource_changes"][0]
    if change in {"create", "update", "delete"}: row["change"]["actions"] = [change]
    if change == "replace": row["change"]["replace_paths"] = [["name"]]
    if change == "unknown": row["change"]["after_unknown"] = {"started": True}
    if change == "incomplete": plan["complete"] = False
    if change == "drift": plan["resource_drift"] = [row]
    if change == "checks": plan["checks"] = [{"status": "fail"}]
    if change == "foreign": row["address"] = "foreign"
    if change == "power": row["change"]["after"]["started"] = True
    if change:
        with pytest.raises(PlanError):
            certification.verify_noop(plan, runner.variables, runner.values(), "stopped")
    else:
        certification.verify_noop(plan, runner.variables, runner.values(), "stopped")


@pytest.mark.parametrize("changed_at", ["claim", "dispatch"])
def test_certificate_admits_historical_instance_and_is_rechecked_before_dispatch(
        historical, sessions, application, client, auth, monkeypatch, changed_at):
    from iacp.api.dependencies import get_power_client, get_power_publisher
    expected, executor, runner, native, planner = historical
    certification.certify(expected, sessions, executor, record=True, client=native)
    application.dependency_overrides[get_power_client] = lambda: lambda: native
    application.dependency_overrides[get_power_publisher] = lambda: Mock()
    monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "1")
    accepted = request(client, auth, expected.instance_id).json()
    def change_binding():
        with sessions() as session:
            row = session.get(TerraformExecution, expected.instance_id)
            row.state_key = "changed-binding"
            session.commit()
    if changed_at == "claim":
        change_binding()
    else:
        original = native.observe
        def observe(*a, **kw):
            result = original(*a, **kw)
            change_binding()
            return result
        monkeypatch.setattr(native, "observe", observe)
    execute_power(accepted["operation_id"], sessions, native)
    result = client.get('/v1/operations/' + accepted["operation_id"], headers=auth).json()
    assert result["status"] == "failed" and result["error_code"] == "POWER_OWNERSHIP_CERTIFICATION_REQUIRED"
    assert not any(call[0] == "submit" for call in native.calls)


def test_certification_serializes_with_terraform_lock(historical, sessions):
    from iacp.terraform.locks import instance_lock, ExecutionBusy
    expected, executor, runner, native, planner = historical
    with instance_lock(sessions.kw["bind"], expected.instance_id):
        with pytest.raises(ExecutionBusy):
            certification.certify(expected, sessions, executor, record=True, client=native)


def test_offline_planner_uses_only_local_state_and_no_credentials(historical, monkeypatch):
    expected, executor, runner, native, planner = historical
    # Exercise production orchestration with a fake CLI and a synthetic archive.
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        data = b'terraform {\n  backend "s3" {}\n}\n'
        member = tarfile.TarInfo("stacks/kvm-instance/versions.tf")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    data = archive.getvalue()
    monkeypatch.setattr(certification.subprocess, "run", lambda *a, **kw: Mock(stdout=data))
    monkeypatch.setattr(certification, "KVM_POWER_SOURCE_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    commands = []
    def run(args, cwd, env, cancelled, **kw):
        commands.append(args)
        assert not any("SECRET" in key or "TOKEN" in key or key.startswith("AWS_ACCESS") for key in env)
        assert 'backend "s3"' not in (cwd / "versions.tf").read_text()
        assert "https://127.0.0.1:1/" in (cwd / "providers.tf").read_text()
        if args[0] == "version":
            return json.dumps({"terraform_version": "1.16.6", "provider_selections": {"registry.terraform.io/bpg/proxmox": "0.116.1"}}).encode()
        if args[0] == "plan":
            assert "-refresh=false" in args
            (cwd / "certification.tfplan").write_bytes(b"synthetic-safe-plan")
        if args[0] == "show":
            if args[-1].endswith("saved.tfplan"):
                return json.dumps(plan_json(runner.variables)).encode()
            return json.dumps(noop(runner.variables, runner.values())).encode()
        return b""
    monkeypatch.setattr(executor.runner, "run", run)
    # Fixture replaces offline_plan; invoke the saved production function below.
    result = ORIGINAL_OFFLINE_PLAN(executor, runner.read(lambda: False), runner.variables, runner.values(), "stopped", lambda: None,
        executor.workspace(expected.instance_id, 4) / "source/stacks/kvm-instance/saved.tfplan")
    assert result["actions"] == ["no-op"]
    assert [row[0] for row in commands] == ["version", "init", "version", "show", "plan", "show"]
    assert "-backend=false" in commands[1]


def test_offline_planner_rejects_wrong_source_archive_before_terraform(historical, monkeypatch):
    expected, executor, runner, native, planner = historical
    monkeypatch.setattr(certification.subprocess, "run", lambda *a, **kw: Mock(stdout=b"wrong-source"))
    with pytest.raises(PlanError, match="POWER_CERTIFICATION_SOURCE_INTEGRITY_FAILED"):
        ORIGINAL_OFFLINE_PLAN(executor, runner.read(lambda: False), runner.variables, runner.values(), "stopped", lambda: None,
            executor.workspace(expected.instance_id, 4) / "source/stacks/kvm-instance/saved.tfplan")
    assert not runner.commands


@pytest.mark.parametrize("revision", ["0" * 40, "f" * 40])
def test_unrecognized_historical_revision_is_rejected(client, auth, sessions, power, revision):
    identifier, native, publisher = power
    with sessions() as session:
        session.get(TerraformExecution, identifier).source_revision = revision
        session.commit()
    assert request(client, auth, identifier).json()["detail"] == "POWER_SOURCE_REVISION_UNSUPPORTED"
    publisher.assert_not_called()
    assert not native.calls


ORIGINAL_OFFLINE_PLAN = certification.offline_plan
