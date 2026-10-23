"""Compatibilidad histórica con artefactos sintéticos; nunca accede a infraestructura."""
from datetime import timedelta
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
import zipfile

import pytest
from sqlalchemy import select

from iacp.api.dependencies import get_apply_publisher
from iacp.models import Instance, Job, Operation, OperationEvent, TerraformApply, TerraformExecution, utcnow
from iacp.terraform.apply import execute_apply
from iacp.terraform.config import Allocation, PlanError, SOURCE_REVISION, TERRAFORM_VERSION
from iacp.terraform.executor import ADDRESS, TerraformExecutor, write_private
from iacp.terraform.locks import instance_lock
from iacp.terraform.state import StateSnapshot
from tests.test_apply import ApplyingExecutor, approval, dispatch, prepared
from tests.test_instances import SPEC, identities
from tests.test_terraform import allocation_data, plan_json, terraform_mode


class ObservationRunner:
    def __init__(self, variables, outcome="existing"):
        self.variables, self.outcome, self.commands = variables, outcome, []
        self.apply_saved = Mock(side_effect=AssertionError("La recuperación nunca aplica"))
        self.state_override = None
        self.proxmox_override = None
        self.reads = []

    def values(self):
        values = plan_json(self.variables)["resource_changes"][0]["change"]["after"]
        values.update(id=str(values["vm_id"]), started=self.variables["instance"]["started"])
        return values

    def state(self):
        return self.state_override if self.state_override is not None else {
            "version": 4, "lineage": "d954af88-6ae6-2d04-9ead-55ed1f95d644", "serial": 3,
            "resources": [] if self.outcome == "empty" else [{
                "module": "module.kvm", "mode": "managed", "type": "proxmox_virtual_environment_vm",
                "name": "this", "instances": [{"attributes": self.values()}]}]}

    def proxmox(self, env, expected, cancelled):
        assert not cancelled()
        return self.proxmox_override if self.proxmox_override is not None else {
            "present": self.outcome != "empty", "power_state": "stopped"}

    def run(self, args, cwd, env, cancelled, allowed=(0,), phase=None):
        self.commands.append(args)
        assert not cancelled()
        if args[0] == "version":
            return json.dumps({"terraform_version": TERRAFORM_VERSION}).encode()
        if args == ["state", "pull"]:
            if self.outcome == "backend-error":
                raise PlanError("TERRAFORM_COMMAND_FAILED", phase=phase, command="state", exit_code=1)
            return json.dumps(self.state()).encode()
        if args[0] == "plan":
            assert args[1] == "-refresh-only"
            if self.outcome == "provider-error":
                raise PlanError("TERRAFORM_COMMAND_FAILED", phase=phase, command="plan", exit_code=1)
        if args[0] == "show":
            if args[-1] == "saved.tfplan":
                return json.dumps(plan_json(self.variables)).encode()
            assert args == ["show", "-json", "observation.tfplan"]
            if self.outcome == "invalid-json":
                return b"not-json"
            values = self.values()
            resources = [] if self.outcome == "empty" else [{"address": ADDRESS, "values": values}]
            # Terraform 1.16.6 refresh-only: no resource_changes/planned resources.
            # prior_state contiene el state refrescado, incluidos los módulos.
            observed = deepcopy(json.loads((Path(__file__).parent / "fixtures/terraform_1_16_6_refresh_only.json").read_text()))
            observed["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"] = resources
            return json.dumps(observed).encode()
        return b""

    def read(self, cancelled, phase="observe.state"):
        # GET Wasabi simulado. El executor de producción usa S3StateReader.
        self.reads.append(phase)
        assert not cancelled()
        if self.outcome == "backend-error":
            raise PlanError("STATE_BACKEND_READ_FAILED", phase=phase)
        return StateSnapshot.parse(json.dumps(self.state()).encode(), "fixture-version-3")

    def close(self):
        pass


def artifacts(tmp_path, monkeypatch, *, identifier=None, attempt=2, allocation=None, variables=None, state_key="iacp/lab/fixture/terraform.tfstate"):
    allocation = allocation or Allocation.model_validate(allocation_data())
    variables = variables or allocation.variables(SimpleNamespace(**SPEC))
    identifier = identifier or str(uuid4())
    runner = ObservationRunner(variables)
    executor = TerraformExecutor(tmp_path, tmp_path / "plans", runner)
    monkeypatch.setattr(executor, "environment", lambda workspace: {})
    monkeypatch.setattr("iacp.terraform.executor.proxmox_resource", runner.proxmox)
    monkeypatch.setattr("iacp.terraform.executor.S3StateReader", lambda backend, env: runner)
    workspace = executor.workspace(identifier, attempt)
    root = workspace / "source/stacks/kvm-instance"
    root.mkdir(parents=True)
    saved = root / "saved.tfplan"
    with zipfile.ZipFile(saved, "w") as archive:
        archive.writestr("tfstate", json.dumps({"version": 4, "lineage": runner.state()["lineage"], "serial": 1}))
        archive.writestr("fixture", "plan-historico-sintetico")
    sha = hashlib.sha256(saved.read_bytes()).hexdigest()
    backend = allocation.backend.arguments(state_key)
    # Esquema exacto anterior a 06c0190: seis campos, sin huellas retrospectivas.
    manifest = {"sha256": sha, "source_revision": SOURCE_REVISION, "state_key": state_key,
                "variables": variables, "backend": backend, "summary": {"apply_enabled": False}}
    write_private(workspace / "manifest.json", manifest)
    write_private(root / "backend.tfbackend.json", backend)
    write_private(root / "iacp.auto.tfvars.json", variables)
    args = (identifier, attempt, sha, state_key, allocation.model_dump(), variables)
    return executor, runner, workspace, root, args


@pytest.mark.parametrize("modern", [False, True])
def test_historical_and_current_manifests_reconcile_without_rewriting(tmp_path, monkeypatch, modern):
    executor, runner, workspace, root, args = artifacts(tmp_path, monkeypatch)
    if modern:
        path = workspace / "manifest.json"
        manifest = json.loads(path.read_text())
        salt = "1" * 64
        # La identidad de lectura puede haber rotado; una nueva mutación sigue bloqueada.
        manifest.update(execution_context_salt=salt,
                        execution_context_sha256=executor.context_digest({"PROXMOX_VE_API_TOKEN": "old-token"}, salt))
        write_private(path, manifest)
    before = {path: path.read_bytes() for path in (workspace / "manifest.json", root / "saved.tfplan")}
    verification = executor.prepare_reconcile(*args)
    assert verification["legacy_manifest"] is (not modern)
    assert verification["plan_sha256"] == args[2] and verification["execution_enabled"] is False
    with pytest.raises(PlanError, match="PLAN_INTEGRITY_FAILED"):
        executor.prepare_apply(*args)
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    boundary = Mock()
    with pytest.raises(PlanError, match="PLAN_INTEGRITY_FAILED"):
        executor.apply(*args, before_mutation=boundary)
    boundary.assert_not_called()
    assert runner.commands == []
    result = executor.observe(args[0], args[1], args[4], args[5], lambda: False)
    assert result["vmid"] == 21001 and result["simulated"] is False
    runner.apply_saved.assert_not_called()
    assert before == {path: path.read_bytes() for path in before}
    assert [command[0] for command in runner.commands] == ["version", "init", "show", "plan", "show"]


@pytest.mark.parametrize("change", ["plan", "approval-sha", "manifest-sha", "missing-plan", "state-key", "backend",
                                   "backend-file", "variables", "variables-file", "revision", "partial-context", "invalid-context", "invalid-manifest"])
def test_tampering_is_rejected_before_any_observation(tmp_path, monkeypatch, change):
    executor, runner, workspace, root, args = artifacts(tmp_path, monkeypatch)
    path = workspace / "manifest.json"
    manifest = json.loads(path.read_text())
    if change == "plan": (root / "saved.tfplan").write_bytes(b"alterado")
    elif change == "approval-sha": args = (*args[:2], "b" * 64, *args[3:])
    elif change == "missing-plan": (root / "saved.tfplan").unlink()
    elif change == "manifest-sha": manifest["sha256"] = "b" * 64
    elif change == "state-key": manifest["state_key"] = "other-state"
    elif change == "backend": manifest["backend"]["bucket"] = "other-bucket"
    elif change == "backend-file": write_private(root / "backend.tfbackend.json", {})
    elif change == "variables": manifest["variables"]["instance"]["vm_id"] = 104
    elif change == "variables-file": write_private(root / "iacp.auto.tfvars.json", {})
    elif change == "revision": manifest["source_revision"] = "0" * 40
    elif change == "partial-context": manifest["execution_context_salt"] = "1" * 64
    elif change == "invalid-context": manifest.update(execution_context_salt=None, execution_context_sha256="invalid")
    elif change == "invalid-manifest": manifest = []
    write_private(path, manifest)
    with pytest.raises(PlanError, match="PLAN_INTEGRITY_FAILED"):
        executor.prepare_reconcile(*args)
    assert runner.commands == []
    runner.apply_saved.assert_not_called()


@pytest.fixture
def historical_run(client, auth, prepared, sessions, monkeypatch, tmp_path):
    with sessions() as session:
        execution = session.get(TerraformExecution, prepared[0]["instance_id"])
        instance = session.get(Instance, execution.instance_id)
        allocation = Allocation.model_validate(execution.allocation)
        executor, runner, workspace, root, args = artifacts(tmp_path, monkeypatch, identifier=instance.id,
            attempt=execution.attempt, allocation=allocation, variables=allocation.variables(instance), state_key=execution.state_key)
        execution.plan_sha256 = args[2]
        session.commit()
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared, body={"attempt": args[1], "plan_sha256": args[2]}).json()
    assert dispatch(client, prepared, auth, row).status_code == 202
    execute_apply(row["approval_id"], sessions, ApplyingExecutor("TERRAFORM_COMMAND_FAILED"))
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "0")
    monkeypatch.setenv("IACP_TERRAFORM_PLAN_ENABLED", "0")
    return row, executor, runner, workspace, root, args


@pytest.mark.parametrize("outcome,expected,error", [("existing", "completed", None), ("empty", "uncertain", "RESOURCE_ABSENT"),
    ("provider-error", "uncertain", "TERRAFORM_COMMAND_FAILED"), ("invalid-json", "uncertain", "INVALID_TERRAFORM_OUTPUT"),
    ("tampered", "uncertain", "PLAN_INTEGRITY_FAILED")])
@pytest.mark.parametrize("admission", ["0", "1"])
def test_recovery_with_apply_disabled_never_applies(client, auth, prepared, historical_run, sessions, application, outcome, expected, error, admission, monkeypatch):
    monkeypatch.setenv("IACP_ADMISSION_ENABLED", admission)
    row, executor, runner, workspace, root, args = historical_run
    runner.outcome = outcome
    if outcome == "tampered": (root / "saved.tfplan").write_bytes(b"alterado")
    publisher = Mock()
    application.dependency_overrides[get_apply_publisher] = lambda: publisher
    path = prepared[2] + "/applies/" + row["approval_id"]
    assert client.post(path + "/recover", headers=auth).status_code == 202
    publisher.assert_called_once_with(row["approval_id"], "reconcile")
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    current = client.get(prepared[2] + "/applies", headers=auth).json()[0]
    assert (current["status"], current["error_code"]) == (expected, error)
    if outcome != "tampered":
        assert current["result"]["reconciliation"]["legacy_manifest"] is True
    else:
        assert runner.commands == []
    runner.apply_saved.assert_not_called()
    instance = client.get("/v1/instances/" + args[0], headers=auth).json()
    assert instance["status"] == ("ready" if expected == "completed" else "failed")
    if outcome == "empty":
        assert instance["vmid"] is None
    # Un mensaje de apply tardío ni siquiera alcanza el adaptador.
    execute_apply(row["approval_id"], sessions, executor)
    runner.apply_saved.assert_not_called()
    if expected == "completed":
        commands = list(runner.commands)
        execute_apply(row["approval_id"], sessions, executor, reconcile=True)
        assert runner.commands == commands


def resolution_body(args):
    return {"attempt": args[1], "plan_sha256": args[2], "reason": "Ausencia revisada en ambas fuentes con escritores detenidos",
        "state": {"bucket": args[4]["backend"]["bucket"], "key": args[3], "version_id": "synthetic-version-1",
                  "lineage": str(uuid4()), "serial": 1, "sha256": "c" * 64, "resource_count": 0,
                  "observed_at": utcnow().isoformat().replace("+00:00", "Z"), "evidence_ref": "INC-FIXTURE/state"},
        "proxmox": {"vmid": args[4]["vm_id"], "node": args[4]["node_name"], "scope": "cluster", "absent": True,
                    "observed_at": utcnow().isoformat().replace("+00:00", "Z"), "evidence_ref": "INC-FIXTURE/proxmox"}}


@pytest.mark.parametrize("drained", [False, True])
def test_audited_absence_closes_as_failed_and_cannot_reuse_authorization(client, auth, prepared, historical_run, sessions, monkeypatch, drained):
    if drained: monkeypatch.setenv("IACP_ADMISSION_ENABLED", "0")
    row, executor, runner, workspace, root, args = historical_run
    runner.outcome = "empty"
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    body = resolution_body(args)
    path = prepared[2] + "/applies/" + row["approval_id"]
    headers = {**prepared[1], "Idempotency-Key": "resolve-1"}
    with instance_lock(sessions.kw["bind"], args[0]):
        assert client.post(path + "/resolve-absence", headers=headers, json=body).status_code == 409
    response = client.post(path + "/resolve-absence", headers=headers, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["status"], result["error_code"]) == ("failed", "RESOURCE_ABSENCE_CONFIRMED")
    assert result["started_at"] is not None
    assert result["result"]["resolution"]["actor"] == "infra-review"
    assert result["result"]["resolution"]["evidence"] == body
    assert client.post(path + "/resolve-absence", headers=headers, json=body).json() == result
    assert client.post(path + "/resolve-absence", headers=headers, json={**body, "reason": "Distinta"}).status_code == 409
    assert client.post(path + "/resolve-absence", headers={**prepared[1], "Idempotency-Key": "other"}, json=body).status_code == 409
    assert client.post(path + "/cancel", headers=auth).json() == result
    assert client.post(path + "/recover", headers=auth).status_code == 409
    instance = client.get("/v1/instances/" + args[0], headers=auth).json()
    assert instance["status"] == "failed" and instance["vmid"] is instance["node"] is None
    assert instance["result"]["phase"] == "resolved_absent"
    with sessions() as session:
        assert session.get(Operation, prepared[0]["operation_id"]).status == "failed"
        assert session.get(Job, result["job_id"]).status == "failed"
        events = session.scalars(select(OperationEvent).where(OperationEvent.code == "terraform_absence_resolved")).all()
        assert len(events) == 1 and events[0].actor == "infra-review"
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    monkeypatch.setenv("IACP_ADMISSION_ENABLED", "1")
    # El replay solo devuelve el run anterior; ninguna clave nueva puede despacharlo.
    assert dispatch(client, prepared, auth, row).json() == result
    assert dispatch(client, prepared, auth, row, key="new-apply").status_code == 409
    assert approval(client, prepared, key="new-approval", body={"attempt": args[1], "plan_sha256": args[2]}).status_code == 409
    execute_apply(row["approval_id"], sessions, executor)
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    runner.apply_saved.assert_not_called()
    # Una decisión futura exige cancelación del plan y otro intento; conserva VMID/state.
    assert client.post(prepared[2] + "/cancel", headers=auth).status_code == 200
    retry = client.post(prepared[2] + "/retry", headers={**auth, "Idempotency-Key": "future-plan"}, json={"expected_attempt": args[1]})
    assert retry.status_code == 202 and retry.json()["attempt"] == args[1] + 1
    with sessions() as session:
        execution = session.get(TerraformExecution, args[0])
        assert execution.state_key == args[3] and execution.vmid == args[4]["vm_id"]


@pytest.mark.parametrize("change", ["unobserved", "provider-error", "tampered", "existing", "wrong-sha", "wrong-bucket", "wrong-key", "wrong-vmid", "wrong-node", "stale", "future", "resources", "present", "no-zone", "operator", "requester"])
def test_absence_resolution_requires_both_evidence_and_verified_reconciliation(client, auth, prepared, historical_run, sessions, change):
    row, executor, runner, workspace, root, args = historical_run
    runner.outcome = change if change in {"provider-error", "existing"} else "empty"
    if change == "tampered": (root / "saved.tfplan").write_bytes(b"alterado")
    if change != "unobserved": execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    body = resolution_body(args)
    if change == "wrong-sha": body["plan_sha256"] = "d" * 64
    if change == "wrong-bucket": body["state"]["bucket"] = "other-bucket"
    if change == "wrong-key": body["state"]["key"] = "other-key"
    if change == "wrong-vmid": body["proxmox"]["vmid"] = 104
    if change == "wrong-node": body["proxmox"]["node"] = "other-node"
    if change == "stale": body["state"]["observed_at"] = (utcnow() - timedelta(days=1)).isoformat()
    if change == "future": body["proxmox"]["observed_at"] = (utcnow() + timedelta(days=1)).isoformat()
    if change == "resources": body["state"]["resource_count"] = 1
    if change == "present": body["proxmox"]["absent"] = False
    if change == "no-zone": body["state"]["observed_at"] = "2026-10-22T00:00:00"
    if change == "requester":
        with sessions() as session:
            session.get(TerraformApply, row["approval_id"]).requested_by = "infra-review"
            session.commit()
    headers = {**(auth if change == "operator" else prepared[1]), "Idempotency-Key": "resolve-1"}
    response = client.post(prepared[2] + "/applies/" + row["approval_id"] + "/resolve-absence", headers=headers, json=body)
    assert response.status_code == (403 if change == "operator" else 422 if change in {"resources", "present", "no-zone"} else 409), response.text
    runner.apply_saved.assert_not_called()
    with sessions() as session:
        assert session.scalar(select(OperationEvent.id).where(OperationEvent.code == "terraform_absence_resolved")) is None


def test_queued_recovery_fences_apply_even_when_reconcile_publication_fails(client, auth, prepared, sessions, application, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared).json()
    assert dispatch(client, prepared, auth, row).status_code == 202
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "0")
    publisher = Mock(side_effect=TimeoutError("private broker endpoint"))
    application.dependency_overrides[get_apply_publisher] = lambda: publisher
    path = prepared[2] + "/applies/" + row["approval_id"] + "/recover"
    with instance_lock(sessions.kw["bind"], prepared[0]["instance_id"]):
        assert client.post(path, headers=auth).status_code == 409
    publisher.assert_not_called()
    assert client.post(path, headers=auth).status_code == 503
    current = client.get(prepared[2] + "/applies", headers=auth).json()[0]
    assert current["status"] == "uncertain" and current["started_at"] is None
    late = ApplyingExecutor()
    execute_apply(row["approval_id"], sessions, late)
    assert late.calls == []
    publisher.side_effect = None
    assert client.post(path, headers=auth).status_code == 202
    assert publisher.call_args_list == [((row["approval_id"], "reconcile"),), ((row["approval_id"], "reconcile"),)]
    observer = ApplyingExecutor("RESOURCE_ABSENT")
    execute_apply(row["approval_id"], sessions, observer, reconcile=True)
    assert observer.calls == []
    assert client.get(prepared[2] + "/applies", headers=auth).json()[0]["error_code"] == "RESOURCE_ABSENT"
