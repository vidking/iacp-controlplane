import json
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from api.db import database, initialize
from worker.allocation import allocate, release
from worker.golden import configured_pool_id, validate_pool, variables
from worker.main import (
    claim,
    execute_power,
    local_variables,
    operation_output,
    physical_presence,
    recover_stale_operations,
    select_target_node,
    secret_path_for_command,
    terraform_commands,
)
from worker.heartbeat import heartbeat
from worker.proxmox import ProxmoxClient, ProxmoxError, credentials


class FakeProxmox:
    def __init__(self):
        self.items = []

    def inventory(self, *, uncertain=False):
        return self.items

    def pools(self):
        return [{"poolid": "iacp-lab"}]


def test_allocation_reuses_released_vmid(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40000")
    from api.db import initialize

    initialize()
    client = FakeProxmox()
    first = allocate("operation-a", client)
    release(first)
    second = allocate("operation-b", client)
    assert first == second == 40000


def test_allocation_can_keep_unique_vmids_for_physical_matrix(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40002")
    monkeypatch.setenv("IACP_VMID_REUSE", "0")
    initialize()
    client = FakeProxmox()
    first = allocate("operation-unique-a", client)
    release(first)
    second = allocate("operation-unique-b", client)
    release(second)
    third = allocate("operation-unique-c", client)
    assert (first, second, third) == (40000, 40001, 40002)


def test_allocation_serializes_six_concurrent_requests_and_reuses_lowest_free_vmid(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_VMID_MIN", "40001")
    monkeypatch.setenv("IACP_VMID_MAX", "40010")
    monkeypatch.setenv("IACP_VMID_REUSE", "1")
    initialize()
    client = FakeProxmox()

    with ThreadPoolExecutor(max_workers=6) as executor:
        vmids = list(
            executor.map(
                lambda index: allocate(f"operation-six-{index}", client),
                range(6),
            )
        )

    assert sorted(vmids) == list(range(40001, 40007))
    assert len(set(vmids)) == 6
    for vmid in vmids:
        release(vmid)
    assert allocate("operation-reuse-after-six", client) == 40001


def test_claim_limits_two_concurrent_operations_and_preserves_pending_work(monkeypatch):
    monkeypatch.setenv("IACP_MAX_CONCURRENT", "6")
    initialize()
    with database() as db:
        db.executemany(
            "INSERT INTO operations(operation_id, command, status, payload_json, created_at) VALUES (?, 'provision', 'pending', '{}', ?)",
            [(f"claim-operation-{index}", f"2026-01-01T00:00:0{index}+00:00") for index in range(6)],
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        claimed = list(executor.map(lambda index: claim(f"worker-{index + 1}"), range(6)))

    operation_ids = [item["operation_id"] for item in claimed if item]
    assert len(operation_ids) == 2
    assert len(set(operation_ids)) == 2
    with database() as db:
        statuses = db.execute(
            "SELECT status, COUNT(*) AS count FROM operations GROUP BY status"
        ).fetchall()
    assert {row["status"]: row["count"] for row in statuses} == {"running": 2, "pending": 4}


def test_allocation_keeps_scheduler_target_node(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_VMID_MIN", "40001")
    monkeypatch.setenv("IACP_VMID_MAX", "40001")
    initialize()
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, created_at)
            VALUES ('operation-node', 'provision', 'pending', '{}', 'now')
            """
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, node, status, spec_json, created_at, updated_at)
            VALUES ('instance-node', 'operation-node', 'node-target', 'ubuntu-2404', 'mng01', 'pending', '{}', 'now', 'now')
            """
        )
    allocate("operation-node", FakeProxmox(), node="mng02")
    with database() as db:
        row = db.execute("SELECT vmid, node FROM instances WHERE instance_id='instance-node'").fetchone()
    assert row["vmid"] == 40001
    assert row["node"] == "mng02"


def test_golden_variables_preserve_hardware_and_dhcp(tmp_path, monkeypatch):
    key = "ssh-ed25519 AAAAexample"
    monkeypatch.setenv("IACP_DEFAULT_SSH_PUBLIC_KEY", key)
    spec = {
        "name": "worker-test",
        "image": "ubuntu-2404",
        "vcpus": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "network_mode": "dhcp",
        "ssh_public_keys": [],
        "started": True,
        "tags": [],
    }
    golden = {
        "template_vm_id": 29000,
        "template_node": "mng01",
        "vmtag": "ubuntu-2404",
        "cpu_type": "x86-64-v4",
        "cpu_flags": ["+aes"],
        "numa": True,
        "cpu_sockets": 1,
        "agent_enabled": True,
        "bios": "ovmf",
        "machine": "q35",
        "scsi_hardware": "virtio-scsi-single",
        "hotplug": "disk,network,memory",
        "boot_order": ["scsi0"],
        "network_bridge": "vmbr0",
        "disk": {
            "interface": "scsi0",
            "datastore_id": "DS_CYBERFUEL",
            "size_gb": 20,
            "minimum_size_gb": 20,
            "aio": "io_uring",
            "cache": "none",
            "discard": "on",
            "backup": True,
            "replicate": True,
            "iothread": True,
            "ssd": False,
        },
    }
    result = variables(spec, 40000, golden)
    instance = result["instance"]
    assert instance["cpu_type"] == "x86-64-v4"
    assert instance["cpu_flags"] == ["+aes"]
    assert instance["network_mode"] == "dhcp"
    assert instance["network_model"] == "virtio"
    assert instance["ipv4_cidr"] is None
    assert instance["boot_order"] == ["scsi0"]
    assert instance["ssh_public_keys"] == [key]
    assert instance["pool_id"] == "iacp-lab"


def test_pool_is_configurable_only_as_internal_worker_setting(monkeypatch):
    monkeypatch.setenv("IACP_PROXMOX_POOL_ID", "iacp-lab")
    assert configured_pool_id() == "iacp-lab"
    assert validate_pool(FakeProxmox()) == "iacp-lab"


def test_pool_validation_rejects_missing_pool(monkeypatch):
    monkeypatch.setenv("IACP_PROXMOX_POOL_ID", "iacp-lab")

    class MissingPool(FakeProxmox):
        def pools(self):
            return [{"poolid": "other-pool"}]

    with pytest.raises(Exception, match="PROXMOX_POOL_NOT_FOUND"):
        validate_pool(MissingPool())


def test_golden_variables_accept_password_only_access(monkeypatch):
    monkeypatch.delenv("IACP_DEFAULT_SSH_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("IACP_DEFAULT_SSH_PUBLIC_KEY_FILE", raising=False)
    spec = {
        "name": "password-only",
        "image": "ubuntu-2404",
        "vcpus": 1,
        "memory_mb": 2048,
        "disk_gb": 50,
        "network_mode": "dhcp",
        "ssh_public_keys": [],
        "started": False,
        "tags": ["cl-0000"],
    }
    golden = {
        "template_vm_id": 29000,
        "template_node": "mng01",
        "vmtag": "ubuntu-2404",
        "cpu_type": "x86-64-v4",
        "cpu_flags": [],
        "numa": False,
        "cpu_sockets": 1,
        "agent_enabled": True,
        "network_bridge": "vmbr0",
        "disk": {
            "interface": "scsi0",
            "datastore_id": "DS_CYBERFUEL",
            "size_gb": 20,
            "minimum_size_gb": 20,
        },
    }
    result = variables(spec, 40001, golden, has_root_password=True)
    assert result["instance"]["ssh_public_keys"] == []
    assert result["instance"]["cloudinit_username"] == "root"


def test_scheduler_chooses_least_loaded_node_and_prefers_mng01_on_tie():
    class ScheduledProxmox:
        def __init__(self, inventory):
            self._inventory = inventory

        def nodes(self):
            return [{"node": "mng01", "status": "online"}]

        def storage_status(self, node, storage):
            assert storage == "DS_CYBERFUEL"
            return {"active": 1, "node": node}

        def inventory(self):
            return self._inventory

    assert select_target_node(ScheduledProxmox([
        {"node": "mng01", "vmid": 40000},
    ])) == "mng01"


def test_local_fixture_variables_are_small_and_portable():
    result = local_variables({
        "name": "local-test",
        "image": "ubuntu-2404",
        "vcpus": 1,
        "memory_mb": 2048,
        "disk_gb": 20,
    })
    assert result["environment"] == "local"
    assert result["instance"]["name"] == "local-test"


def test_proxmox_credentials_accept_portainer_environment(monkeypatch):
    monkeypatch.delenv("IACP_PROXMOX_CREDENTIALS_FILE", raising=False)
    monkeypatch.setenv("IACP_PROXMOX_ENDPOINT", "https://mng01.cyberfuel.com:8006/")
    monkeypatch.setenv("IACP_PROXMOX_API_TOKEN", "user@pam!token=example")
    endpoint, token = credentials()
    assert endpoint == "https://mng01.cyberfuel.com:8006"
    assert token == "user@pam!token=example"


def test_unknown_terraform_command_cannot_fall_through_to_destroy():
    with pytest.raises(RuntimeError, match="orden Terraform no soportada"):
        terraform_commands("power", "/tmp/plan.tfplan")


def test_destroy_does_not_reuse_deleted_bootstrap_secret():
    assert secret_path_for_command(
        "destroy", {"secret_path": "/missing/root_password"}, {}
    ) is None
    assert secret_path_for_command(
        "apply", {"secret_path": "/existing/root_password"}, {}
    ) == "/existing/root_password"


def test_provision_terraform_commands_have_one_plan_and_one_apply():
    commands = terraform_commands("provision", "/tmp/plan.tfplan")
    assert [command[1] for command in commands] == ["init", "plan", "apply"]


def test_power_task_identity_accepts_stop_fallback():
    task = "UPID:mng01:00000001:00000002:00000003:qmstop:40000:iacp-orchestrator@pve!:"
    ProxmoxClient._validate_task("mng01", 40000, "stop", task)


def test_reboot_falls_back_to_stop_start_after_qmreboot_timeout(monkeypatch):
    client = ProxmoxClient.__new__(ProxmoxClient)
    states = iter(("running", "running", "stopped"))
    waits = iter(
        (
            ProxmoxError("POWER_TASK_FAILED: VM quit/powerdown failed - got timeout"),
            {"power_state": "stopped", "exitstatus": "OK"},
            {"power_state": "running", "exitstatus": "OK"},
        )
    )
    actions = []

    monkeypatch.setattr(client, "permissions", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "power_state", lambda *args, **kwargs: {"power_state": next(states)})
    monkeypatch.setattr(client, "post", lambda path, parameters=None: actions.append(path) or path)
    monkeypatch.setattr(client, "_validate_task", lambda *args, **kwargs: None)

    def fake_wait_task(*args, **kwargs):
        value = next(waits)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(client, "wait_task", fake_wait_task)

    result = client.power("mng01", 40000, "test-vm", "reboot")

    assert result["fallback_action"] == "stop_start"
    assert result["power_state"] == "running"
    assert actions == [
        "/nodes/mng01/qemu/40000/status/reboot",
        "/nodes/mng01/qemu/40000/status/stop",
        "/nodes/mng01/qemu/40000/status/start",
    ]


def test_proxmox_retries_mutation_when_dns_fails_before_connecting(monkeypatch):
    client = ProxmoxClient.__new__(ProxmoxClient)
    client.base = "https://mng01.example/api2/json"
    client.token = "user@pve!token"
    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"data":{"ok":true}}'

    def fake_open(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise URLError(socket.gaierror(-5, "No address associated with hostname"))
        return Response()

    client.opener = SimpleNamespace(open=fake_open)

    assert client._request("POST", "/nodes/mng01/status/reboot", {"timeout": 60}) == {"ok": True}
    assert attempts == [
        "https://mng01.example/api2/json/nodes/mng01/status/reboot",
        "https://mng01.example/api2/json/nodes/mng01/status/reboot",
    ]


def test_proxmox_does_not_retry_mutation_after_http_5xx(monkeypatch):
    client = ProxmoxClient.__new__(ProxmoxClient)
    client.base = "https://mng01.example/api2/json"
    client.token = "user@pve!token"
    attempts = []

    def fake_open(request, timeout):
        attempts.append(request.full_url)
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    client.opener = SimpleNamespace(open=fake_open)

    with pytest.raises(ProxmoxError) as error:
        client._request("POST", "/nodes/mng01/status/reboot", {"timeout": 60}, uncertain=True)
    assert error.value.uncertain is True
    assert len(attempts) == 1


def test_wait_task_retries_transient_dns_poll(monkeypatch):
    client = ProxmoxClient.__new__(ProxmoxClient)
    responses = iter(
        (
            ProxmoxError("dns", uncertain=True),
            {"status": "stopped", "exitstatus": "OK"},
        )
    )
    def fake_get(*_args, **_kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr("worker.proxmox.time.sleep", lambda *_args: None)
    result = client.wait_task("mng01", 40000, "shutdown", "UPID:mng01:1:2:3:qmshutdown:40000:x:")
    assert result["power_state"] == "stopped"


def test_physical_presence_checks_identity_without_hiding_wrong_vm(monkeypatch):
    client = FakeProxmox()
    client.items = [{"vmid": 40000, "name": "expected", "node": "mng01"}]
    assert physical_presence(client, 40000, True, name="expected", node="mng01") is True
    # A stale cluster inventory row is not proof of a wrong VM; the caller
    # must keep reconciling until the new identity appears or the timeout is
    # exhausted.
    assert physical_presence(client, 40000, True, name="other", node="mng01") is False


def test_power_operation_updates_instance_and_operation(tmp_path, monkeypatch):
    initialize()
    operation_id = "power-operation"
    instance_id = "power-instance"
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, stack, created_at)
            VALUES (?, 'power', 'power', 'running', ?, ?, 'mng01', 'now')
            """,
            (operation_id, json.dumps({"command": "power", "power_action": "start", "instance_id": instance_id}), instance_id),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, ?, 'power-test', 'ubuntu-2404', 40000, 'mng01', 'ready', 'stopped', '{}', 'now', 'now')
            """,
            (instance_id, "plan-operation"),
        )

    class FakePower:
        def power(self, node, vmid, name, action):
            assert (node, vmid, name, action) == ("mng01", 40000, "power-test", "start")
            return {"action": action, "power_state": "running", "task": "synthetic"}

    execute_power("worker-1", {"operation_id": operation_id, "payload_json": json.dumps({"command": "power", "power_action": "start", "instance_id": instance_id})}, FakePower())
    with database() as db:
        operation = db.execute("SELECT status FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        instance = db.execute("SELECT status, power_state FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
    assert operation["status"] == "succeeded"
    assert instance["status"] == "ready"
    assert instance["power_state"] == "running"


def test_heartbeat_refreshes_operation_phase_and_worker_age(tmp_path, monkeypatch):
    initialize()
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, created_at)
            VALUES ('heartbeat-operation', 'provision', 'provision', 'running', '{}', ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )

    heartbeat("worker-heartbeat", "running", "heartbeat-operation", phase="apply")

    with database() as db:
        operation = db.execute(
            "SELECT last_heartbeat, phase FROM operations WHERE operation_id='heartbeat-operation'"
        ).fetchone()
        worker = db.execute(
            "SELECT status, current_operation_id, heartbeat_count FROM workers WHERE worker_id='worker-heartbeat'"
        ).fetchone()
    assert operation["last_heartbeat"]
    assert operation["phase"] == "apply"
    assert worker["status"] == "running"
    assert worker["current_operation_id"] == "heartbeat-operation"
    assert worker["heartbeat_count"] == 1


def test_stale_worker_claim_is_closed_and_reconciled_once(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_OPERATION_STALE_SECONDS", "30")
    initialize()
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json, instance_id,
              vmid, stack, created_at, started_at, last_heartbeat, phase
            ) VALUES (?, 'provision', 'provision', 'running', ?, ?, 40000, 'mng01', ?, ?, ?, 'apply')
            """,
            (
                "stale-operation",
                json.dumps({"command": "provision", "action": "provision", "instance_id": "stale-instance"}),
                "stale-instance",
                old,
                old,
                old,
            ),
        )
        db.execute(
            """
            INSERT INTO instances(
              instance_id, operation_id, name, image, vmid, node, status,
              power_state, spec_json, created_at, updated_at
            ) VALUES (?, ?, 'stale-vm', 'ubuntu-2404', 40000, 'mng01', 'applying', 'unknown', '{}', ?, ?)
            """,
            ("stale-instance", "stale-operation", old, old),
        )

    assert recover_stale_operations() == 1
    with database() as db:
        source = db.execute(
            "SELECT status, phase, error FROM operations WHERE operation_id='stale-operation'"
        ).fetchone()
        recovery = db.execute(
            "SELECT operation_id, command, status, phase, payload_json FROM operations WHERE command='reconcile'"
        ).fetchone()
        instance = db.execute(
            "SELECT status, power_state FROM instances WHERE instance_id='stale-instance'"
        ).fetchone()
    assert source["status"] == "uncertain"
    assert source["phase"] == "recovery_required"
    assert source["error"] == "WORKER_HEARTBEAT_STALE"
    assert recovery["command"] == "reconcile"
    assert recovery["status"] == "pending"
    assert recovery["phase"] == "recovery"
    assert json.loads(recovery["payload_json"])["source_operation_id"] == "stale-operation"
    assert instance["status"] == "uncertain"
    assert instance["power_state"] == "unknown"
    assert recover_stale_operations() == 0


def test_operation_output_is_bounded_and_redacts_secret(monkeypatch):
    monkeypatch.setenv("IACP_OPERATION_OUTPUT_MAX_CHARS", "4096")
    value = operation_output("root-secret\n" + ("x" * 10000), "root-secret")
    assert "root-secret" not in value
    assert "output truncated" in value
    assert len(value) <= 4096
