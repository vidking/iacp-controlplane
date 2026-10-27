"""Regresiones sin red: state S3, HTTP Proxmox y procesos Terraform simulados."""
from copy import deepcopy
import io
import hashlib
import json
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from iacp.models import TerraformApply
from iacp.terraform.apply import execute_apply
from iacp.terraform.config import PlanError
from iacp.terraform.executor import state_resource
from iacp.terraform.lifecycle import build_executor
from iacp.terraform.observation import NoRedirect, proxmox_resource
from iacp.terraform.runner import CommandRunner
from iacp.terraform.state import StateSnapshot
from tests.test_apply import ApplyingExecutor, approval, dispatch, prepared
from tests.test_historical_recovery import artifacts, historical_run, resolution_body
from tests.test_terraform import terraform_mode


@pytest.mark.parametrize("state_present,proxmox_present,error", [
    (True, True, None), (False, False, "RESOURCE_ABSENT"),
    (False, True, "UNMANAGED_RESOURCE"), (True, False, "STATE_RESOURCE_MISSING"),
])
def test_two_source_matrix_is_persisted_and_never_reapplies(
        client, auth, prepared, historical_run, sessions, state_present, proxmox_present, error):
    row, executor, runner, workspace, root, args = historical_run
    if not state_present:
        runner.state_override = {**runner.state(), "resources": []}
    runner.proxmox_override = {"present": proxmox_present, "power_state": "stopped"}
    path = prepared[2] + "/executions/" + row["approval_id"]
    assert client.post(path + "/recover", headers=auth).status_code == 202
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    current = client.get(prepared[2] + "/executions", headers=auth).json()[0]
    assert current["error_code"] == error
    assert current["status"] == ("uncertain" if error else "completed")
    assert current["result"]["reconciliation"]["workspace"] == "default"
    assert current["result"]["reconciliation"]["state_key"] == args[3]
    if error:
        diagnostic = current["result"]["diagnostic"]
        assert diagnostic["phase"] == "observe.compare"
        assert diagnostic["state_present"] == state_present and diagnostic["proxmox_present"] == proxmox_present
        assert diagnostic["state_consistency"]["change"] == "unchanged"
        if error != "RESOURCE_ABSENT":
            assert client.post(path + "/resolve-absence", headers={**prepared[1], "Idempotency-Key": "reject-partial"},
                               json=resolution_body(args)).status_code == 409
    else:
        assert current["result"]["observation"]["values_source"] == "prior_state.values"
    reads = len(runner.reads)
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    assert (len(runner.reads) == reads) is (not error)
    execute_apply(row["approval_id"], sessions, executor)  # entrega apply atrasada
    runner.apply_saved.assert_not_called()
    assert hashlib.sha256((root / "saved.tfplan").read_bytes()).hexdigest() == args[2]


def test_backend_unreachable_is_observation_error(client, auth, prepared, historical_run, sessions):
    row, executor, runner, _, _, _ = historical_run
    runner.outcome = "backend-error"
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    current = client.get(prepared[2] + "/executions", headers=auth).json()[0]
    assert current["status"] == "uncertain" and current["error_code"] == "STATE_BACKEND_READ_FAILED"
    assert current["result"]["diagnostic"] == {"phase": "observe.state"}
    assert not any(command[0] == "plan" for command in runner.commands)


@pytest.mark.parametrize("change", ["id", "vm_id", "missing-id", "node", "name", "foreign", "duplicate", "tainted", "deposed", "indexed", "invalid", "null-resources"])
def test_raw_module_state_never_adopts_partial_or_foreign_identity(tmp_path, monkeypatch, change):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    state = deepcopy(runner.state())
    resource = state["resources"][0]
    instance = resource["instances"][0]
    if change == "id": instance["attributes"]["id"] = "999"
    if change == "vm_id": instance["attributes"]["vm_id"] = 999
    if change == "missing-id": del instance["attributes"]["id"]
    if change == "node": instance["attributes"]["node_name"] = "mng02"
    if change == "name": instance["attributes"]["name"] = "foreign"
    if change == "foreign": resource["module"] = "module.other"
    if change == "duplicate": state["resources"].append(deepcopy(resource))
    if change == "tainted": instance["status"] = "tainted"
    if change == "deposed": instance["deposed"] = "deadbeef"
    if change == "indexed": instance["index_key"] = 0
    if change == "invalid": state = {"error": "secret-backend-response"}
    if change == "null-resources": state["resources"] = None
    with pytest.raises(PlanError) as failure:
        state_resource(state, args[5])
    assert failure.value.code != "RESOURCE_ABSENT"
    assert failure.value.phase == "observe.state"
    assert "secret" not in json.dumps(failure.value.diagnostic())


def test_tainted_state_is_only_readable_by_explicit_destructive_recovery(tmp_path, monkeypatch):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    state = deepcopy(runner.state())
    state["resources"][0]["instances"][0]["status"] = "tainted"
    with pytest.raises(PlanError) as failure:
        state_resource(state, args[5])
    assert failure.value.code == "INCOMPLETE_OBSERVATION"
    recovered = state_resource(state, args[5], allow_tainted=True)
    assert recovered["vm_id"] == args[5]["instance"]["vm_id"]


@pytest.mark.parametrize("change,error", [("empty", "PROVIDER_OBSERVATION_INCONSISTENT"),
    ("partial", "INCOMPLETE_OBSERVATION"), ("errored", "INCOMPLETE_OBSERVATION"),
    ("actions", "INCOMPLETE_OBSERVATION"), ("missing-prior", "PROVIDER_OBSERVATION_INCONSISTENT"),
    ("disk", "OBSERVED_RESOURCE_MISMATCH"), ("cpu", "OBSERVED_RESOURCE_MISMATCH"),
    ("memory", "OBSERVED_RESOURCE_MISMATCH"), ("id", "OBSERVED_RESOURCE_MISMATCH")])
def test_present_in_both_requires_complete_consistent_provider_refresh(tmp_path, monkeypatch, change, error):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    run = runner.run
    def changed(command, *arguments, **kwargs):
        data = run(command, *arguments, **kwargs)
        if command == ["show", "-json", "observation.tfplan"]:
            value = json.loads(data)
            if change == "empty": value["prior_state"]["values"] = {}
            elif change == "partial": value["complete"] = False
            elif change == "errored": value["errored"] = True
            elif change == "actions": value["resource_changes"] = [{"change": {"actions": ["create"]}}]
            elif change == "missing-prior": del value["prior_state"]
            else:
                actual = value["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]["values"]
                if change == "disk": actual["disk"][0]["size"] += 1
                if change == "cpu": actual["cpu"][0]["cores"] += 1
                if change == "memory": actual["memory"][0]["dedicated"] += 1
                if change == "id": actual["id"] = "999"
            return json.dumps(value).encode()
        return data
    monkeypatch.setattr(runner, "run", changed)
    with pytest.raises(PlanError, match=error):
        executor.observe(args[0], args[1], args[4], args[5], lambda: False)
    runner.apply_saved.assert_not_called()


def test_backend_change_during_observation_keeps_uncertainty(tmp_path, monkeypatch):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    original = runner.read
    def changed(cancelled, phase="observe.state"):
        snapshot = original(cancelled, phase)
        if phase == "observe.state_verify":
            return StateSnapshot.parse(json.dumps({**snapshot.state, "serial": 4}).encode(), "fixture-version-4")
        return snapshot
    monkeypatch.setattr(runner, "read", changed)
    with pytest.raises(PlanError, match="STATE_CHANGED_DURING_OBSERVATION"):
        executor.observe(args[0], args[1], args[4], args[5], lambda: False)


@pytest.fixture
def native_api(monkeypatch):
    env = {"PROXMOX_VE_ENDPOINT": "https://fixture.invalid:8006/", "PROXMOX_VE_API_TOKEN": "fixture@pve!reader=secret"}
    expected = {"vm_id": 21001, "node_name": "mng01", "vm_name": "fixture-vm"}
    inventory = [{"vmid": 21001, "node": "mng01", "name": "fixture-vm", "type": "qemu", "status": "stopped"}]
    config = {"name": "fixture-vm"}
    permissions = {"/vms": {"VM.Audit": 1}}
    responses = {"/cluster/resources?type=vm": inventory, "/nodes/mng01/qemu/21001/config": config,
                 "/access/permissions?path=/vms": permissions}
    calls = []
    def open_request(request, timeout):
        calls.append(request)
        assert request.method == "GET" and timeout == 30
        assert request.get_header("Authorization") == "PVEAPIToken=" + env["PROXMOX_VE_API_TOKEN"]
        path = request.full_url.split("/api2/json", 1)[1]
        value = responses[path]
        if isinstance(value, Exception): raise value
        stream = io.BytesIO(json.dumps({"data": value}).encode())
        stream.status = 200
        return stream
    opener = Mock(open=Mock(side_effect=open_request))
    monkeypatch.setattr("iacp.terraform.observation.build_opener", Mock(return_value=opener))
    return env, expected, responses, calls


@pytest.mark.parametrize("status", [401, 403, 404, 500, 502, 503])
@pytest.mark.parametrize("phase_path", ["/cluster/resources?type=vm", "/nodes/mng01/qemu/21001/config"])
def test_proxmox_http_failures_never_mean_absence(native_api, status, phase_path):
    env, expected, responses, _ = native_api
    responses[phase_path] = HTTPError("https://secret.invalid", status, "secret-provider-response", {}, None)
    with pytest.raises(PlanError, match="PROXMOX_OBSERVATION_FAILED") as failure:
        proxmox_resource(env, expected, lambda: False)
    assert failure.value.diagnostic() == {"phase": "observe.proxmox", "http_status": status}
    assert "secret" not in str(failure.value.diagnostic())


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_proxmox_failure_is_persisted_as_uncertain(client, auth, prepared, historical_run, sessions, monkeypatch, native_api, status):
    row, executor, runner, _, _, _ = historical_run
    env, _, responses, _ = native_api
    responses["/cluster/resources?type=vm"] = HTTPError("https://secret.invalid", status, "secret-provider-response", {}, None)
    monkeypatch.setattr(executor, "environment", lambda *args: env)
    monkeypatch.setattr("iacp.terraform.executor.proxmox_resource", proxmox_resource)
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    current = client.get(prepared[2] + "/executions", headers=auth).json()[0]
    assert current["status"] == "uncertain" and current["error_code"] == "PROXMOX_OBSERVATION_FAILED"
    assert current["result"]["diagnostic"] == {"phase": "observe.proxmox", "http_status": status, "state_present": True}
    assert "secret" not in json.dumps(current)
    runner.apply_saved.assert_not_called()


@pytest.mark.parametrize("scope", [None, 0, 1])
def test_proxmox_absence_requires_propagated_cluster_audit(native_api, scope):
    env, expected, responses, _ = native_api
    responses["/cluster/resources?type=vm"] = []
    responses["/access/permissions?path=/vms"] = {} if scope is None else {"/vms": {"VM.Audit": scope}}
    if scope == 1:
        assert proxmox_resource(env, expected, lambda: False) == {"present": False}
    else:
        with pytest.raises(PlanError, match="PROXMOX_OBSERVATION_SCOPE_INSUFFICIENT"):
            proxmox_resource(env, expected, lambda: False)


@pytest.mark.parametrize("change,error", [("present", None), ("network", "PROXMOX_OBSERVATION_FAILED"),
    ("node", "OBSERVED_RESOURCE_MISMATCH"), ("name", "OBSERVED_RESOURCE_MISMATCH"),
    ("lock", "RESOURCE_OPERATION_IN_PROGRESS"), ("invalid", "PROXMOX_OBSERVATION_FAILED"),
    ("invalid-vmid", "PROXMOX_OBSERVATION_FAILED"),
    ("cancel", "CANCELLED")])
def test_native_observation_identity_scope_and_failures(native_api, change, error):
    env, expected, responses, calls = native_api
    if change == "network": responses["/cluster/resources?type=vm"] = URLError("secret-network-message")
    if change == "node": responses["/cluster/resources?type=vm"][0]["node"] = "mng02"
    if change == "name": responses["/nodes/mng01/qemu/21001/config"]["name"] = "other-vm"
    if change == "lock": responses["/nodes/mng01/qemu/21001/config"]["lock"] = "clone"
    if change == "invalid": responses["/cluster/resources?type=vm"] = {"error": "secret"}
    if change == "invalid-vmid": responses["/cluster/resources?type=vm"][0]["vmid"] = "not-an-id"
    if error:
        with pytest.raises(PlanError, match=error):
            proxmox_resource(env, expected, lambda: change == "cancel")
    else:
        assert proxmox_resource(env, expected, lambda: False) == {"present": True, "power_state": "stopped"}
        assert len(calls) == 2
    assert NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


def test_production_factory_observes_with_plan_and_apply_disabled(client, auth, prepared, historical_run, sessions, monkeypatch):
    row, executor, runner, workspace, _, _ = historical_run
    monkeypatch.setenv("IACP_TERRAFORM_SOURCE_DIR", str(executor.source))
    monkeypatch.setenv("IACP_TERRAFORM_ARTIFACTS_DIR", str(executor.artifacts))
    # Usar factory real (antes devolvía REAL_PLAN_DISABLED), proceso y HTTP simulados.
    monkeypatch.setattr("iacp.terraform.executor.CommandRunner", lambda: runner)
    monkeypatch.setattr("iacp.terraform.executor.TerraformExecutor.environment", lambda *args: {})
    execute_apply(row["approval_id"], sessions, reconcile=True)
    current = client.get(prepared[2] + "/executions", headers=auth).json()[0]
    assert current["status"] == "completed"
    with pytest.raises(PlanError, match="REAL_PLAN_DISABLED"):
        build_executor()
    runner.apply_saved.assert_not_called()


def test_recovery_rereads_state_after_interrupted_response(client, auth, prepared, historical_run, sessions):
    row, executor, runner, _, _, _ = historical_run
    runner.outcome = "backend-error"
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    runner.outcome = "existing"
    previous = len(runner.commands)
    assert client.post(prepared[2] + "/executions/" + row["approval_id"] + "/recover", headers=auth).status_code == 202
    execute_apply(row["approval_id"], sessions, executor, reconcile=True)
    current = client.get(prepared[2] + "/executions", headers=auth).json()[0]
    assert current["status"] == "completed" and current["error_code"] is None
    assert len(runner.commands) > previous
    runner.apply_saved.assert_not_called()


def test_crash_between_observation_and_database_commit_is_recoverable(client, auth, prepared, sessions, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    row = approval(client, prepared).json()
    assert dispatch(client, prepared, auth, row).status_code == 202
    class InterruptedExecutor(ApplyingExecutor):
        def observe(self, *args):
            super().observe(*args)
            raise SystemExit("simulated worker exit before result transaction")
    interrupted = InterruptedExecutor()
    with pytest.raises(SystemExit):
        execute_apply(row["approval_id"], sessions, interrupted)
    with sessions() as session:
        run = session.get(TerraformApply, row["approval_id"])
        assert run.status == "running" and run.started_at is not None
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "0")
    assert client.post(prepared[2] + "/executions/" + row["approval_id"] + "/recover", headers=auth).status_code == 202
    observer = ApplyingExecutor()
    execute_apply(row["approval_id"], sessions, observer, reconcile=True)
    assert client.get(prepared[2] + "/executions", headers=auth).json()[0]["status"] == "completed"
    assert len(interrupted.calls) == 1 and observer.calls == []


def test_old_absence_result_cannot_close_without_new_two_source_observation(client, auth, prepared, historical_run, sessions):
    row, _, _, _, _, args = historical_run
    execute_apply(row["approval_id"], sessions, ApplyingExecutor("RESOURCE_ABSENT"), reconcile=True)
    response = client.post(prepared[2] + "/executions/" + row["approval_id"] + "/resolve-absence",
                           headers={**prepared[1], "Idempotency-Key": "old-absence"}, json=resolution_body(args))
    assert response.status_code == 409 and response.json()["detail"] == "ABSENCE_NOT_CONFIRMED"


def test_runner_allows_exact_read_only_state_pull_and_rejects_variants(tmp_path, monkeypatch):
    runner = CommandRunner()
    execute = Mock(return_value=b"{}")
    monkeypatch.setattr(runner, "_execute", execute)
    runner.run(["state", "pull"], tmp_path, {}, lambda: False)
    execute.assert_called_once()
    for args in (["state", "push"], ["state", "rm"], ["import"], ["state", "pull", "-ignore-remote-version"]):
        with pytest.raises(PlanError, match="COMMAND_FORBIDDEN"):
            runner.run(args, tmp_path, {}, lambda: False)


def test_state_provider_and_native_api_share_worker_credentials_and_workspace(tmp_path, monkeypatch):
    executor, runner, workspace, _, args = artifacts(tmp_path, monkeypatch)
    credentials = {"AWS_ACCESS_KEY_ID": "fixture-key", "AWS_SECRET_ACCESS_KEY": "fixture-secret",
                   "PROXMOX_VE_API_TOKEN": "fixture@pve!reader=fixture-token",
                   "PROXMOX_VE_ENDPOINT": "https://fixture.invalid:8006/"}
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(credentials))
    monkeypatch.setenv("IACP_TERRAFORM_CREDENTIALS_FILE", str(path))
    monkeypatch.setenv("TF_WORKSPACE", "wrong-workspace")
    monkeypatch.setenv("TF_CLI_ARGS", "-lock=false")
    monkeypatch.delattr(executor, "environment")  # usar la construcción real del entorno
    native = Mock(side_effect=runner.proxmox)
    monkeypatch.setattr("iacp.terraform.executor.proxmox_resource", native)
    environments = []
    run = runner.run
    def capture(command, cwd, env, *arguments, **kwargs):
        environments.append(env)
        assert env["TF_WORKSPACE"] == "default" and "TF_CLI_ARGS" not in env
        assert env["TF_DATA_DIR"] == str(workspace / "data")
        assert all(env[key] == value for key, value in credentials.items())
        return run(command, cwd, env, *arguments, **kwargs)
    monkeypatch.setattr(runner, "run", capture)
    verification = executor.prepare_reconcile(*args)
    result = executor.observe(args[0], args[1], args[4], args[5], lambda: False)
    assert result["vmid"] == args[4]["vm_id"] and verification["state_key"] == args[3]
    assert all(env is native.call_args.args[0] for env in environments)

@pytest.mark.parametrize('bad',[None,'tag=0','tag=204','trunks=204','gateway'])
def test_dhcp_native_configuration_requires_omitted_tag_and_gateway(native_api,bad):
    env,expected,responses,_=native_api
    expected.update(network_mode='dhcp',vlan_id=None,network_bridge='vmbr0')
    config=responses['/nodes/mng01/qemu/21001/config']
    config.update(net0='virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0',ipconfig0='ip=dhcp')
    if bad=='gateway':config['ipconfig0']+=',gw=192.0.2.1'
    elif bad:config['net0']+=','+bad
    if bad:
        with pytest.raises(PlanError,match='DHCP_NATIVE_CONFIGURATION_MISMATCH'):proxmox_resource(env,expected,lambda:False)
    else:assert proxmox_resource(env,expected,lambda:False)['present'] is True
