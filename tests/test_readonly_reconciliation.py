"""Regresiones de consistencia; todas las identidades y servicios son sintéticos."""
from copy import deepcopy
import io
import json
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

from botocore.exceptions import EndpointConnectionError
from botocore.response import StreamingBody
from botocore.stub import Stubber
import pytest
from sqlalchemy import select

from iacp.models import OperationEvent, TerraformApply, TerraformExecution
from iacp.terraform.apply import _execute, execute_apply
from iacp.terraform.config import PlanError
from iacp.terraform.state import S3StateReader, StateSnapshot, canonical_state
from tests.test_apply import prepared
from tests.test_historical_recovery import historical_run
from iacp.terraform.config import Allocation
from tests.test_instances import SPEC
from tests.test_terraform import allocation_data, terraform_mode

from tests.test_historical_recovery import artifacts


def test_same_state_with_permuted_check_results_is_not_a_concurrent_write(tmp_path, monkeypatch):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    run = runner.run
    reads = 0

    def reordered(command, *arguments, **kwargs):
        nonlocal reads
        data = run(command, *arguments, **kwargs)
        if command == ["state", "pull"]:
            reads += 1
            value = json.loads(data)
            checks = [{"object_kind": "var", "config_addr": "var." + name, "status": "pass",
                       "objects": [{"object_addr": "var." + name, "status": "pass"}]}
                      for name in ("instance", "environment", "allowed_nodes")]
            value["check_results"] = checks if reads == 1 else list(reversed(checks))
            return json.dumps(value).encode()
        return data

    monkeypatch.setattr(runner, "run", reordered)
    result = executor.observe(args[0], args[1], args[4], args[5], lambda: False)
    assert result["vmid"] == args[4]["vm_id"]
    assert ["state", "pull"] not in runner.commands
    runner.apply_saved.assert_not_called()


def observe(executor, args):
    return executor.observe(args[0], args[1], args[4], args[5], lambda: False)


def test_canonicalization_retains_checks_and_positional_provider_lists(tmp_path, monkeypatch):
    _, runner, _, _, _ = artifacts(tmp_path, monkeypatch)
    first = runner.state()
    first["check_results"] = [{"config_addr": name, "status": "pass", "objects": [
        {"object_addr": "b", "status": "pass"}, {"object_addr": "a", "status": "pass"}]}
        for name in ("var.instance", "var.environment")]
    second = deepcopy(first)
    second["check_results"].reverse()
    second["check_results"][0]["objects"].reverse()
    assert first != second and canonical_state(first) == canonical_state(second)
    second["check_results"][0]["status"] = "fail"
    assert canonical_state(first) != canonical_state(second)
    before = {"resources": [{"instances": [{"attributes": {"ordered": [1, 2]}}]}]}
    after = {"resources": [{"instances": [{"attributes": {"ordered": [2, 1]}}]}]}
    assert canonical_state(before) != canonical_state(after)


@pytest.mark.parametrize("change,classification", [
    ("serial", "serial_advanced"), ("regression", "stale_or_regressed"),
    ("lineage", "lineage_changed"), ("attribute", "content_changed"),
    ("version", "version_rewrite"), ("representation", "representation_rewrite"),
    ("same-version-new-bytes", "inconsistent_version_read"),
])
def test_identified_snapshot_conflicts_never_confirm(tmp_path, monkeypatch, change, classification):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    original = runner.read
    def changed(cancelled, phase="observe.state"):
        snapshot = original(cancelled, phase)
        if phase != "observe.state_verify":
            return snapshot
        state = deepcopy(snapshot.state)
        version = "fixture-version-4"
        if change == "serial": state["serial"] += 1
        if change == "regression": state["serial"] -= 1
        if change == "lineage": state["lineage"] = "other-lineage"
        if change in {"attribute", "same-version-new-bytes"}:
            state["resources"][0]["instances"][0]["attributes"]["description"] = "changed"
        if change == "same-version-new-bytes": version = snapshot.version_id
        data = json.dumps(state, indent=2 if change == "representation" else None).encode()
        return StateSnapshot.parse(data, version)
    monkeypatch.setattr(runner, "read", changed)
    with pytest.raises(PlanError, match="STATE_CHANGED_DURING_OBSERVATION") as failure:
        observe(executor, args)
    diagnostic = failure.value.diagnostic()
    assert diagnostic["state_consistency"]["change"] == classification
    assert diagnostic["state_present"] is diagnostic["proxmox_present"] is True
    assert set(diagnostic["state_consistency"]["before"]) == {"version_id", "lineage", "serial", "sha256", "content_sha256"}
    runner.apply_saved.assert_not_called()


@pytest.mark.parametrize("version", [None, "", "null"])
def test_unversioned_backend_cannot_claim_aba_protection(tmp_path, monkeypatch, version):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "read", lambda *a: StateSnapshot.parse(json.dumps(runner.state()).encode(), version))
    with pytest.raises(PlanError, match="STATE_VERSION_UNAVAILABLE"):
        observe(executor, args)


@pytest.fixture
def s3_reader(tmp_path, monkeypatch):
    _, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    env = {"AWS_ACCESS_KEY_ID": "synthetic-key", "AWS_SECRET_ACCESS_KEY": "synthetic-secret"}
    reader = S3StateReader(args[4]["backend"] | {"endpoints": {"s3": args[4]["backend"]["endpoint"]}, "key": args[3]}, env)
    with Stubber(reader.client) as stub:
        yield reader, stub, json.dumps(runner.state()).encode()
        stub.assert_no_pending_responses()
    reader.close()


def test_s3_reads_bytes_and_version_together_and_closes_body(s3_reader):
    reader, stub, data = s3_reader
    body = StreamingBody(io.BytesIO(data), len(data))
    stub.add_response("get_object", {"Body": body, "ContentLength": len(data), "VersionId": "version-3",
                      "ResponseMetadata": {"HTTPStatusCode": 200}}, {"Bucket": reader.bucket, "Key": reader.key})
    result = reader.read(lambda: False)
    assert result.version_id == "version-3" and result.state["serial"] == 3
    assert body._raw_stream.closed


@pytest.mark.parametrize("code,status,expected", [
    ("AccessDenied", 403, "STATE_BACKEND_READ_FAILED"), ("InternalError", 500, "STATE_BACKEND_READ_FAILED"),
    ("NoSuchBucket", 404, "STATE_BACKEND_READ_FAILED"), ("NoSuchKey", 404, "STATE_BACKEND_OBJECT_MISSING"),
])
def test_backend_errors_never_mean_resource_absence(s3_reader, code, status, expected):
    reader, stub, _ = s3_reader
    stub.add_client_error("get_object", service_error_code=code, service_message="secret-backend-message", http_status_code=status)
    with pytest.raises(PlanError, match=expected) as failure:
        reader.read(lambda: False)
    assert "secret" not in json.dumps(failure.value.diagnostic())
    assert failure.value.state_present is None


def test_backend_connectivity_error_and_cancellation(s3_reader, monkeypatch):
    reader, _, _ = s3_reader
    request = Mock(side_effect=EndpointConnectionError(endpoint_url="https://secret.invalid"))
    monkeypatch.setattr(reader.client, "get_object", request)
    with pytest.raises(PlanError, match="CANCELLED"):
        reader.read(lambda: True)
    request.assert_not_called()
    with pytest.raises(PlanError, match="STATE_BACKEND_READ_FAILED"):
        reader.read(lambda: False)


@pytest.mark.parametrize("data", [b'{}', b'{"serial":1,"serial":2}', b'{"x":NaN}', b'not-json', b'[]'])
def test_malformed_backend_state_is_not_absence(data):
    with pytest.raises(PlanError, match="INVALID_TERRAFORM_OUTPUT"):
        StateSnapshot.parse(data, "version")


@pytest.mark.parametrize("source", ["state", "provider"])
@pytest.mark.parametrize("attribute", ["bridge", "vlan", "ipv4", "gateway", "username", "ssh-key", "agent", "power", "pool", "disk-cache", "null-value"])
def test_all_known_approved_postconditions_are_required(tmp_path, monkeypatch, source, attribute):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    changed = runner.values()
    if attribute == "bridge": changed["network_device"][0]["bridge"] = "other-bridge"
    if attribute == "vlan": changed["network_device"][0]["vlan_id"] += 1
    if attribute == "ipv4": changed["initialization"][0]["ip_config"][0]["ipv4"][0]["address"] = "192.0.2.99/24"
    if attribute == "gateway": changed["initialization"][0]["ip_config"][0]["ipv4"][0]["gateway"] = "192.0.2.99"
    if attribute == "username": changed["initialization"][0]["user_account"][0]["username"] = "other"
    if attribute == "ssh-key": changed["initialization"][0]["user_account"][0]["keys"] = []
    if attribute == "agent": changed["agent"][0]["enabled"] = True
    if attribute == "power": changed["started"] = True
    if attribute == "pool": changed["pool_id"] = "other-pool"
    if attribute == "disk-cache": changed["disk"][0]["cache"] = "unsafe"
    if attribute == "null-value": del changed["disk"][0]["file_format"]
    if source == "state":
        state = runner.state()
        state["resources"][0]["instances"][0]["attributes"] = changed
        runner.state_override = state
    else:
        original = runner.run
        def run(command, *a, **kw):
            result = original(command, *a, **kw)
            if command == ["show", "-json", "observation.tfplan"]:
                value = json.loads(result)
                value["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]["values"] = changed
                return json.dumps(value).encode()
            return result
        monkeypatch.setattr(runner, "run", run)
    with pytest.raises(PlanError, match="APPROVED_POSTCONDITION_MISMATCH"):
        observe(executor, args)
    runner.apply_saved.assert_not_called()


@pytest.mark.parametrize("status", ["fail", "unknown", "error"])
def test_refresh_checks_must_pass(tmp_path, monkeypatch, status):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    original = runner.run
    def run(command, *a, **kw):
        result = original(command, *a, **kw)
        if command == ["show", "-json", "observation.tfplan"]:
            value = json.loads(result)
            value["checks"] = [{"status": status}]
            return json.dumps(value).encode()
        return result
    monkeypatch.setattr(runner, "run", run)
    with pytest.raises(PlanError, match="APPROVED_POSTCONDITION_MISMATCH"):
        observe(executor, args)


def test_recovery_with_apply_enabled_and_duplicate_workers_is_serialized(
        client, auth, prepared, historical_run, sessions, monkeypatch):
    row, executor, runner, _, _, _ = historical_run
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    entered, release = Event(), Event()
    original = executor.observe
    def blocking(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(executor, "observe", blocking)
    errors = []
    def first():
        try:
            execute_apply(row["approval_id"], sessions, executor, reconcile=True)
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=first)
    worker.start()
    try:
        assert entered.wait(10)
        competitor = Mock()
        execute_apply(row["approval_id"], sessions, competitor, reconcile=True)
        competitor.observe.assert_not_called()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    before = list(runner.commands)
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    execute_apply(row["approval_id"], sessions, executor)
    assert runner.commands == before
    assert all(command[0] in {"version", "init", "show", "plan"} for command in runner.commands)
    assert all(command[1] == "-refresh-only" for command in runner.commands if command[0] == "plan")
    runner.apply_saved.assert_not_called()
    with sessions() as session:
        assert session.get(TerraformApply, row["approval_id"]).status == "completed"
        assert len(session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id == prepared[0]["operation_id"], OperationEvent.code == "terraform_apply_completed")).all()) == 1


def test_conflict_then_recovery_reobserves_instead_of_reusing_evidence(
        client, auth, prepared, historical_run, sessions, monkeypatch):
    row, executor, runner, _, _, _ = historical_run
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    original = runner.read
    def changed(cancelled, phase="observe.state"):
        snapshot = original(cancelled, phase)
        if phase == "observe.state_verify":
            return StateSnapshot.parse(json.dumps(snapshot.state).encode(), "fixture-version-rewrite")
        return snapshot
    monkeypatch.setattr(runner, "read", changed)
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    with sessions() as session:
        current = session.get(TerraformApply, row["approval_id"])
        assert current.status == "uncertain" and current.error_code == "STATE_CHANGED_DURING_OBSERVATION"
    monkeypatch.setattr(runner, "read", original)
    before = len(runner.reads)
    assert client.post(prepared[2] + "/applies/" + row["approval_id"] + "/recover", headers=auth).status_code == 202
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    assert len(runner.reads) == before + 2
    with sessions() as session:
        current = session.get(TerraformApply, row["approval_id"])
        assert current.status == "completed" and current.error_code is None
        assert current.result["reconciliation"]["approval_id"] == row["approval_id"]
    runner.apply_saved.assert_not_called()


def test_backend_failure_on_verification_never_commits_success(
        client, auth, prepared, historical_run, sessions, monkeypatch):
    row, executor, runner, _, _, _ = historical_run
    original = runner.read
    def failed(cancelled, phase="observe.state"):
        if phase == "observe.state_verify":
            raise PlanError("STATE_BACKEND_READ_FAILED", phase=phase)
        return original(cancelled, phase)
    monkeypatch.setattr(runner, "read", failed)
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    with sessions() as session:
        current = session.get(TerraformApply, row["approval_id"])
        assert current.status == "uncertain" and current.error_code == "STATE_BACKEND_READ_FAILED"
        assert current.result["diagnostic"]["phase"] == "observe.state_verify"
    runner.apply_saved.assert_not_called()


@pytest.mark.parametrize("change", ["cancel", "artifacts", "binding"])
def test_final_commit_cannot_override_a_changed_operation(
        client, auth, prepared, historical_run, sessions, monkeypatch, change):
    row, executor, runner, _, root, args = historical_run
    original = executor.observe
    def changed(*arguments):
        result = original(*arguments)
        if change == "cancel":
            assert client.post(prepared[2] + "/applies/" + row["approval_id"] + "/cancel", headers=auth).status_code == 200
        if change == "artifacts": (root / "saved.tfplan").write_bytes(b"changed-fixture")
        if change == "binding":
            with sessions() as session:
                session.get(TerraformExecution, args[0]).plan_sha256 = "f" * 64
                session.commit()
        return result
    monkeypatch.setattr(executor, "observe", changed)
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    with sessions() as session:
        current = session.get(TerraformApply, row["approval_id"])
        assert current.status == "uncertain"
        assert current.error_code == {"cancel": "RECONCILIATION_REQUIRED", "artifacts": "PLAN_INTEGRITY_FAILED",
                                      "binding": "PLAN_CHANGED"}[change]
        assert session.scalar(select(OperationEvent.id).where(OperationEvent.code == "terraform_apply_completed")) is None
    runner.apply_saved.assert_not_called()


def test_lost_instance_lock_prevents_terminal_database_commit(
        client, auth, prepared, historical_run, sessions, monkeypatch):
    row, executor, runner, _, _, _ = historical_run
    original = executor.observe
    lost = False
    def observed(*arguments):
        nonlocal lost
        result = original(*arguments)
        lost = True
        return result
    def heartbeat():
        if lost:
            raise RuntimeError("Instance lock connection lost")
    monkeypatch.setattr(executor, "observe", observed)
    with pytest.raises(RuntimeError, match="lock connection lost"):
        _execute(row["approval_id"], sessions, executor, True, heartbeat)
    with sessions() as session:
        assert session.get(TerraformApply, row["approval_id"]).status == "running"
        assert session.scalar(select(OperationEvent.id).where(OperationEvent.code == "terraform_apply_completed")) is None
    runner.apply_saved.assert_not_called()


def test_unknown_computed_fields_do_not_mask_known_postconditions(tmp_path, monkeypatch):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    original = runner.run
    def run(command, *a, **kw):
        result = original(command, *a, **kw)
        if command == ["show", "-json", "saved.tfplan"]:
            value = json.loads(result)
            change = value["resource_changes"][0]["change"]
            change["after"]["id"] = None
            change["after_unknown"] = {"id": True}
            return json.dumps(value).encode()
        return result
    monkeypatch.setattr(runner, "run", run)
    assert observe(executor, args)["observation"]["postconditions_verified"] is True
    runner.proxmox_override = {"present": True, "power_state": "running"}
    with pytest.raises(PlanError, match="APPROVED_POSTCONDITION_MISMATCH"):
        observe(executor, args)


def test_attempt_four_identity_is_observable_without_new_provisioning(tmp_path, monkeypatch):
    # Identificadores del caso, evidencia de infraestructura exclusivamente sintética.
    identifier = "a1b2d063-6240-4969-bc04-e7e0e6be9e73"
    allocation = Allocation.model_validate(allocation_data(vmid=106))
    variables = allocation.variables(SimpleNamespace(**(SPEC | {"name": "iacp-kvm-lab-01"})))
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch, identifier=identifier, attempt=4,
                                            allocation=allocation, variables=variables)
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    result = observe(executor, args)
    assert (result["vmid"], result["node"], result["name"]) == (106, "mng01", "iacp-kvm-lab-01")
    assert result["observation"]["instance_id"] == identifier and result["observation"]["attempt"] == 4
    assert result["observation"]["state_consistency"]["before"]["lineage"] == "d954af88-6ae6-2d04-9ead-55ed1f95d644"
    runner.apply_saved.assert_not_called()


@pytest.mark.parametrize("problem", ["oversized", "truncated", "not-json"])
def test_s3_invalid_bodies_fail_closed_and_are_closed(s3_reader, monkeypatch, problem):
    reader, stub, data = s3_reader
    if problem == "oversized": monkeypatch.setattr("iacp.terraform.state.LIMIT", 8)
    if problem == "not-json": data = b"not-json"
    length = len(data) + (5 if problem == "truncated" else 0)
    body = StreamingBody(io.BytesIO(data), length)
    stub.add_response("get_object", {"Body": body, "ContentLength": length, "VersionId": "version",
                                    "ResponseMetadata": {"HTTPStatusCode": 200}})
    with pytest.raises(PlanError) as failure:
        reader.read(lambda: False, "observe.state_verify")
    assert failure.value.code in {"OUTPUT_LIMIT_EXCEEDED", "STATE_BACKEND_READ_FAILED", "INVALID_TERRAFORM_OUTPUT"}
    assert failure.value.phase == "observe.state_verify"
    assert body._raw_stream.closed


@pytest.mark.parametrize("change", ["lineage", "older-serial"])
def test_stable_state_must_belong_to_the_approved_snapshot(tmp_path, monkeypatch, change):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    state = runner.state()
    if change == "lineage": state["lineage"] = "replacement-lineage"
    else: state["serial"] = 0
    runner.state_override = state
    with pytest.raises(PlanError, match="APPROVED_STATE_MISMATCH"):
        observe(executor, args)
    assert runner.commands == []
    runner.apply_saved.assert_not_called()


@pytest.mark.parametrize("content", [b'{}', b'not-json', b'{"version":4,"lineage":"","serial":1}'])
def test_approved_plan_embedded_state_is_validated(tmp_path, monkeypatch, content):
    from iacp.terraform.executor import approved_state_metadata
    saved = tmp_path / "synthetic-plan.zip"
    with zipfile.ZipFile(saved, "w") as archive:
        archive.writestr("tfstate", content)
    with pytest.raises(PlanError, match="PLAN_INTEGRITY_FAILED"):
        approved_state_metadata(saved)
