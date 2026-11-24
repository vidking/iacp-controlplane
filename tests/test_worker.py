import base64
import json
import re
import struct
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote

import pytest

from api.db import database, initialize
from worker.allocation import allocate, release
from worker.golden import configured_pool_id, validate_pool, variables
from worker.main import (
    claim,
    destroy_validation_password,
    execute,
    execute_power,
    execute_reconcile,
    ensure_vm_running_if_requested,
    guest_identity_previously_verified,
    instance_password_was_provisioned,
    local_variables,
    operation_output,
    physical_presence,
    require_free_provision_target,
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
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40000")
    from api.db import initialize

    initialize()
    client = FakeProxmox()
    first = allocate("operation-a", client)
    release(first, "operation-a")
    second = allocate("operation-b", client)
    assert first == second == 40000


def test_allocation_can_keep_unique_vmids_for_physical_matrix(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40002")
    monkeypatch.setenv("IACP_VMID_REUSE", "0")
    initialize()
    client = FakeProxmox()
    first = allocate("operation-unique-a", client)
    release(first, "operation-unique-a")
    second = allocate("operation-unique-b", client)
    release(second, "operation-unique-b")
    third = allocate("operation-unique-c", client)
    assert (first, second, third) == (40000, 40001, 40002)


def test_allocation_serializes_six_concurrent_requests_and_reuses_lowest_free_vmid(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
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
    for index, vmid in enumerate(vmids):
        release(vmid, f"operation-six-{index}")
    assert allocate("operation-reuse-after-six", client) == 40001


def test_claim_serializes_six_workers_without_duplicate_operations(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
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
    assert len(operation_ids) == 6
    assert len(set(operation_ids)) == 6
    with database() as db:
        statuses = db.execute(
            "SELECT status, COUNT(*) AS count FROM operations GROUP BY status"
        ).fetchall()
    assert {row["status"]: row["count"] for row in statuses} == {"running": 6}


def test_allocation_keeps_scheduler_target_node(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
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


@pytest.mark.parametrize("protected_vmid", [104, 106, 29000, 29012, 29013, 29014, 29015, 29016, 29017])
def test_allocation_never_reserves_a_protected_vmid(tmp_path, monkeypatch, protected_vmid):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_VMID_MIN", str(protected_vmid))
    monkeypatch.setenv("IACP_VMID_MAX", str(protected_vmid))
    initialize()

    with pytest.raises(RuntimeError, match="VMID_RANGE_EXHAUSTED"):
        allocate(f"operation-protected-{protected_vmid}", FakeProxmox())


def test_allocation_honors_additional_protected_vmids(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_VMID_MIN", "40555")
    monkeypatch.setenv("IACP_VMID_MAX", "40555")
    monkeypatch.setenv("IACP_ADDITIONAL_PROTECTED_VMIDS", "40555,not-a-vmid,0")
    initialize()

    with pytest.raises(RuntimeError, match="VMID_RANGE_EXHAUSTED"):
        allocate("operation-additional-protected", FakeProxmox())


@pytest.mark.parametrize("protected_vmid", [104, 106, 29000, 29012, 29013, 29014, 29015, 29016, 29017])
def test_worker_power_rejects_protected_vmid_before_proxmox_mutation(tmp_path, monkeypatch, protected_vmid):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    operation_id = f"protected-power-operation-{protected_vmid}"
    instance_id = f"protected-power-instance-{protected_vmid}"
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, created_at)
            VALUES (?, 'power', 'power', 'running', ?, ?, 'now')
            """,
            (
                operation_id,
                json.dumps({"command": "power", "power_action": "start", "instance_id": instance_id}),
                instance_id,
            ),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, 'protected-source', 'protected-vm', 'ubuntu-2404', ?, 'mng01', 'ready', 'stopped', '{}', 'now', 'now')
            """,
            (instance_id, protected_vmid),
        )

    class MustNotMutate:
        def power(self, *_args, **_kwargs):
            pytest.fail("el worker intentó modificar Proxmox para una VM protegida")

    with pytest.raises(RuntimeError, match="PROTECTED_INSTANCE"):
        execute_power(
            "worker-1",
            {
                "operation_id": operation_id,
                "payload_json": json.dumps(
                    {"command": "power", "power_action": "start", "instance_id": instance_id}
                ),
            },
            MustNotMutate(),
        )

    with database() as db:
        instance = db.execute(
            "SELECT status, vmid, power_state FROM instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
    assert (instance["status"], instance["vmid"], instance["power_state"]) == (
        "ready", protected_vmid, "stopped"
    )


@pytest.mark.parametrize("protected_vmid", [104, 106, 29000, 29012, 29013, 29014, 29015, 29016, 29017])
def test_worker_reconcile_rejects_protected_vmid_before_proxmox_observation(tmp_path, monkeypatch, protected_vmid):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    source_id = f"protected-reconcile-source-{protected_vmid}"
    operation_id = f"protected-reconcile-operation-{protected_vmid}"
    instance_id = f"protected-reconcile-instance-{protected_vmid}"
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, vmid, stack, created_at)
            VALUES (?, 'provision', 'provision', 'uncertain', ?, ?, ?, 'mng01', 'now')
            """,
            (
                source_id,
                json.dumps({"command": "provision", "instance_id": instance_id, "spec": {"started": True}}),
                instance_id,
                protected_vmid,
            ),
        )
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, stack, created_at)
            VALUES (?, 'reconcile', 'reconcile', 'running', ?, ?, 'mng01', 'now')
            """,
            (
                operation_id,
                json.dumps({"command": "reconcile", "instance_id": instance_id, "source_operation_id": source_id}),
                instance_id,
            ),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, ?, 'protected-vm', 'ubuntu-2404', ?, 'mng01', 'uncertain', 'unknown', '{}', 'now', 'now')
            """,
            (instance_id, source_id, protected_vmid),
        )

    class MustNotObserve:
        def __init__(self):
            self.calls = []

        def set_heartbeat(self, _callback):
            self.calls.append("heartbeat")

        def inventory(self, **_kwargs):
            self.calls.append("inventory")
            pytest.fail("el worker consultó Proxmox para una VM protegida")

    client = MustNotObserve()
    with pytest.raises(RuntimeError, match="PROTECTED_INSTANCE"):
        execute_reconcile(
            "worker-1",
            {
                "operation_id": operation_id,
                "payload_json": json.dumps(
                    {"command": "reconcile", "instance_id": instance_id, "source_operation_id": source_id}
                ),
            },
            client,
        )
    assert client.calls == []


@pytest.mark.parametrize("command", ["apply", "destroy"])
@pytest.mark.parametrize("protected_vmid", [104, 106, 29000, 29012, 29013, 29014, 29015, 29016, 29017])
def test_worker_apply_destroy_fail_closed_for_protected_vms(tmp_path, monkeypatch, command, protected_vmid):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    instance_id = f"protected-{command}-instance-{protected_vmid}"
    source_id = f"protected-{command}-source-{protected_vmid}"
    operation_id = f"protected-{command}-operation-{protected_vmid}"
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, plan_id, stack, instance_id, vmid, created_at)
            VALUES (?, 'provision', 'provision', 'succeeded', ?, 'kvm-2', 'mng01', ?, ?, 'now')
            """,
            (
                source_id,
                json.dumps({"command": "provision", "action": "provision", "spec": {"started": True}}),
                instance_id,
                protected_vmid,
            ),
        )
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, plan_operation_id, plan_id, stack, instance_id, created_at)
            VALUES (?, ?, ?, 'running', ?, ?, 'kvm-2', 'mng01', ?, 'now')
            """,
            (
                operation_id,
                command,
                command,
                json.dumps(
                    {
                        "command": command,
                        "action": command,
                        "instance_id": instance_id,
                        "plan_operation_id": source_id,
                        "stack": "mng01",
                    }
                ),
                source_id,
                instance_id,
            ),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, ?, 'protected-vm', 'ubuntu-2404', ?, 'mng01', 'ready', 'stopped', '{}', 'now', 'now')
            """,
            (instance_id, source_id, protected_vmid),
        )

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("la operación protegida alcanzó un cliente remoto o Terraform")

    monkeypatch.setattr("worker.main.load_environment", unexpected_call)
    monkeypatch.setattr("worker.main.ProxmoxClient", unexpected_call)
    monkeypatch.setattr("worker.main.run_commands", unexpected_call)
    execute(
        "worker-1",
        {
            "operation_id": operation_id,
            "command": command,
            "action": command,
            "status": "running",
            "stack": "mng01",
            "instance_id": instance_id,
            "payload_json": json.dumps(
                {
                    "command": command,
                    "action": command,
                    "instance_id": instance_id,
                    "plan_operation_id": source_id,
                    "stack": "mng01",
                }
            ),
        },
    )

    with database() as db:
        operation = db.execute(
            "SELECT status, error FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        instance = db.execute(
            "SELECT status, vmid FROM instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
    assert operation["status"] == "failed"
    assert operation["error"] == "PROTECTED_INSTANCE"
    assert (instance["status"], instance["vmid"]) == ("ready", protected_vmid)


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
    assert instance["started"] is False

    monkeypatch.setenv("IACP_PROD_STATIC_BRIDGE", "vmbr3")
    static_spec = {
        **spec,
        "network_profile": "prod_static",
        "network_mode": "static",
        "dns_servers": ["1.1.1.1", "8.8.8.8"],
        "vlan_id": None,
    }
    static_instance = variables(static_spec, 40001, golden)["instance"]
    assert static_instance["network_bridge"] == "vmbr3"
    assert static_instance["vlan_id"] is None
    assert static_instance["dns_servers"] == ["1.1.1.1", "8.8.8.8"]
    assert static_instance["started"] is False


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


def test_proxmox_encodes_raw_ssh_key_at_form_boundary_with_quotes_and_reserved_chars():
    key_material = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
    key_material += bytes(range(0xF8, 0x100)) * 4
    key_blob = base64.b64encode(key_material).decode("ascii")
    public_key = f"ssh-ed25519 {key_blob} equipo 'red' \"$HOME\""
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":{"ok":true}}'

    def fake_open(request, timeout):
        captured["content_type"] = request.get_header("Content-type")
        captured["body"] = request.data.decode("ascii")
        return Response()

    client = ProxmoxClient.__new__(ProxmoxClient)
    client.base = "https://mng01.example/api2/json"
    client.token = "user@pve!token"
    client.opener = SimpleNamespace(open=fake_open)

    assert client.put(
        "/nodes/mng01/qemu/29010/config", {"sshkeys": public_key}
    ) == {"ok": True}
    assert captured["content_type"] == "application/x-www-form-urlencoded"
    pve_value = parse_qs(captured["body"])["sshkeys"][0]
    assert pve_value == quote(public_key, safe="")
    assert re.fullmatch(r"[-%a-zA-Z0-9_.!~*'()]*", pve_value)
    assert all(character not in pve_value for character in ("+", "/", "=", "'", '"'))
    assert unquote(pve_value) == public_key


def test_proxmox_rejects_ssh_key_values_with_line_return_or_binary_data():
    client = ProxmoxClient.__new__(ProxmoxClient)
    client.base = "https://mng01.example/api2/json"
    client.token = "user@pve!token"

    for value in ("ssh-ed25519 AAAA\rBBBB", b"ssh-ed25519 AAAA"):
        with pytest.raises(ProxmoxError, match="PROXMOX_SSH_KEYS_INVALID"):
            client.put("/nodes/mng01/qemu/29010/config", {"sshkeys": value})


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


def test_proxmox_classifies_http_4xx_mutation_as_definite_rejection(monkeypatch):
    client = ProxmoxClient.__new__(ProxmoxClient)
    client.base = "https://mng01.example/api2/json"
    client.token = "user@pve!token"

    def fake_open(request, timeout):
        raise HTTPError(request.full_url, 400, "parameter verification failed", {}, None)

    client.opener = SimpleNamespace(open=fake_open)

    with pytest.raises(ProxmoxError) as error:
        client._request("POST", "/nodes/mng01/qemu/30000/agent/exec", {"command": "/usr/bin/true"}, uncertain=True)
    assert error.value.uncertain is False
    assert error.value.status == 400


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


def test_physical_absence_uses_cluster_nextid_not_acl_dependent_node_probes():
    from worker.main import PhysicalObservation, _physical_absence_observation

    class Cluster:
        def __init__(self, nextid):
            self.paths = []
            self.nextid = nextid

        def inventory(self, *, uncertain=False):
            return []

        def get(self, path, **_kwargs):
            self.paths.append(path)
            if isinstance(self.nextid, Exception):
                raise self.nextid
            return self.nextid

    libre = Cluster("40000")
    assert physical_presence(libre, 40000, False, node="mng01") is True
    assert _physical_absence_observation(libre, 40000, node="mng01") is PhysicalObservation.ABSENT
    # Una sola consulta, sin sondeos por nodo que Proxmox rechaza con 403.
    assert libre.paths[-1] == "/cluster/nextid?vmid=40000"

    existe = Cluster(ProxmoxError("PROXMOX_REJECTED", 400))
    assert physical_presence(existe, 40000, False, node="mng01") is False
    assert _physical_absence_observation(existe, 40000, node="mng01") is PhysicalObservation.PRESENT

    ambiguo = Cluster(ProxmoxError("PROXMOX_FORBIDDEN", 403))
    assert _physical_absence_observation(ambiguo, 40000, node="mng01") is PhysicalObservation.UNKNOWN


def test_physical_absence_fails_closed_without_a_clear_answer():
    class Cluster:
        def __init__(self, answer):
            self.answer = answer

        def inventory(self, *, uncertain=False):
            return []

        def get(self, path, **_kwargs):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    for respuesta in (None, "", "40001", {"vmid": 40000}, ProxmoxError("TIMEOUT"), ProxmoxError("X", 403)):
        assert physical_presence(Cluster(respuesta), 40000, False, node="mng01") is False
    # Si el inventario ya muestra la VM, ni siquiera se consulta nextid.
    class Visible(Cluster):
        def inventory(self, *, uncertain=False):
            return [{"vmid": 40000, "name": "x", "node": "mng01"}]

        def get(self, path, **_kwargs):
            raise AssertionError("no debe consultar nextid con la VM visible")

    assert physical_presence(Visible("40000"), 40000, False, node="mng01") is False


def test_free_vmid_guard_distinguishes_presence_from_unverifiable_absence(monkeypatch):
    import worker.main as worker_main

    monkeypatch.setattr(worker_main, "assert_lease_owner", lambda *_args: None)
    monkeypatch.setattr(worker_main.time, "sleep", lambda *_args: None)

    monkeypatch.setattr(
        worker_main,
        "_physical_absence_observation",
        lambda *_args, **_kwargs: worker_main.PhysicalObservation.PRESENT,
    )
    with pytest.raises(ProxmoxError, match="VMID_PHYSICAL_RESOURCE_ALREADY_PRESENT"):
        require_free_provision_target(FakeProxmox(), 40855, "operation", "mng01")

    monkeypatch.setattr(
        worker_main,
        "_physical_absence_observation",
        lambda *_args, **_kwargs: worker_main.PhysicalObservation.UNKNOWN,
    )
    with pytest.raises(ProxmoxError, match="VMID_PHYSICAL_ABSENCE_UNCONFIRMED"):
        require_free_provision_target(FakeProxmox(), 40855, "operation", "mng01")

    monkeypatch.setattr(
        worker_main,
        "_physical_absence_observation",
        lambda *_args, **_kwargs: worker_main.PhysicalObservation.ABSENT,
    )
    assert require_free_provision_target(FakeProxmox(), 40855, "operation", "mng01") is None


def test_power_permission_zero_is_denied():
    from worker.proxmox import ProxmoxClient

    client = object.__new__(ProxmoxClient)
    client.get = lambda *_args, **_kwargs: {
        "/vms/40000": {"VM.Audit": 1, "VM.PowerMgmt": 0}
    }
    with pytest.raises(ProxmoxError, match="POWER_PERMISSION_DENIED"):
        client.permissions("mng01", 40000, mutate=True)


def _cliente_con_permisos(respuestas):
    from worker.proxmox import ProxmoxClient

    cliente = object.__new__(ProxmoxClient)
    consultas = []

    def get(ruta, **_kwargs):
        consultas.append(ruta)
        for prefijo, valor in respuestas.items():
            if ruta.startswith(prefijo):
                return valor
        return {}

    cliente.get = get
    cliente.consultas = consultas
    return cliente


def test_power_permission_is_inherited_from_the_pool_for_member_vms(monkeypatch):
    monkeypatch.setenv("IACP_PROXMOX_POOL_ID", "iacp-lab")
    # Caso real de MNG: 0 sobre /vms/<id>, 1 en el pool y la VM es miembro.
    cliente = _cliente_con_permisos({
        "/access/permissions?path=%2Fvms%2F30000": {"/vms/30000": {"VM.Audit": 0, "VM.PowerMgmt": 0}},
        "/pools/iacp-lab": {"members": [{"vmid": 29010}, {"vmid": 30000}]},
        "/access/permissions?path=%2Fpool%2Fiacp-lab": {"/pool/iacp-lab": {"VM.Audit": 1, "VM.PowerMgmt": 1}},
    })
    cliente.permissions("mng01", 30000, mutate=True)


def test_power_permission_denies_vms_outside_the_pool(monkeypatch):
    monkeypatch.setenv("IACP_PROXMOX_POOL_ID", "iacp-lab")
    cliente = _cliente_con_permisos({
        "/access/permissions?path=%2Fvms%2F29000": {"/vms/29000": {"VM.Audit": 0, "VM.PowerMgmt": 0}},
        "/pools/iacp-lab": {"members": [{"vmid": 30000}]},
        "/access/permissions?path=%2Fpool%2Fiacp-lab": {"/pool/iacp-lab": {"VM.Audit": 1, "VM.PowerMgmt": 1}},
    })
    with pytest.raises(ProxmoxError, match="POWER_PERMISSION_DENIED"):
        cliente.permissions("mng01", 29000, mutate=True)
    # Ni siquiera se consulta el privilegio del pool para una VM que no pertenece a él.
    assert not any("pool%2Fiacp-lab" in ruta for ruta in cliente.consultas)


def test_power_permission_denies_when_the_pool_privilege_is_zero(monkeypatch):
    monkeypatch.setenv("IACP_PROXMOX_POOL_ID", "iacp-lab")
    cliente = _cliente_con_permisos({
        "/access/permissions?path=%2Fvms%2F30000": {"/vms/30000": {"VM.Audit": 0, "VM.PowerMgmt": 0}},
        "/pools/iacp-lab": {"members": [{"vmid": 30000}]},
        "/access/permissions?path=%2Fpool%2Fiacp-lab": {"/pool/iacp-lab": {"VM.Audit": 1, "VM.PowerMgmt": 0}},
    })
    with pytest.raises(ProxmoxError, match="POWER_PERMISSION_DENIED"):
        cliente.permissions("mng01", 30000, mutate=True)
    # Lectura: basta VM.Audit.
    cliente.permissions("mng01", 30000, mutate=False)


def test_power_permission_rejects_an_invalid_pool_name(monkeypatch):
    monkeypatch.setenv("IACP_PROXMOX_POOL_ID", "../etc")
    cliente = _cliente_con_permisos({
        "/access/permissions?path=%2Fvms%2F30000": {"/vms/30000": {"VM.Audit": 0, "VM.PowerMgmt": 0}},
    })
    with pytest.raises(ProxmoxError, match="POWER_PERMISSION_DENIED"):
        cliente.permissions("mng01", 30000, mutate=True)
    assert cliente.consultas == ["/access/permissions?path=%2Fvms%2F30000"]


@pytest.mark.no_database
def test_lifecycle_uses_saved_provision_time_image_expectation(tmp_path, monkeypatch):
    import worker.main as worker_main
    from worker.preboot import save_expectation

    workspace = tmp_path / "plans" / "operation" / "environments" / "mng01"
    workspace.mkdir(parents=True)
    expected = {
        "instance": {"vm_name": "saved-contract"},
        "image_id": "ubuntu-2404",
        "node": "mng01",
        "vmid": 40000,
    }
    save_expectation(str(workspace), expected)
    monkeypatch.setattr(worker_main, "instance_plan_operation", lambda _instance_id: "operation")
    monkeypatch.setattr(
        worker_main,
        "find_operation",
        lambda _operation_id: {
            "command": "provision",
            "stack": "mng01",
            "workspace_path": str(workspace.parents[1]),
        },
    )
    monkeypatch.setattr(
        worker_main,
        "expected_preboot_for_spec",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("consultó Golden Image vigente")),
    )

    result = worker_main.lifecycle_preboot_expectation(
        "instance",
        {"image": "ubuntu-2404", "name": "saved-contract"},
        vmid=40000,
        node="mng01",
        client=object(),
        stack="mng01",
        password_present=True,
    )

    assert result == expected


def test_power_start_fails_closed_without_a_saved_request_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
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
        def power(self, *_args):
            pytest.fail("no se permite iniciar una VM sin el contrato original")

    with pytest.raises(ProxmoxError, match="PREBOOT_EXPECTATION_REQUIRED"):
        execute_power(
            "worker-1",
            {"operation_id": operation_id, "payload_json": json.dumps({"command": "power", "power_action": "start", "instance_id": instance_id})},
            FakePower(),
        )
    with database() as db:
        operation = db.execute("SELECT status FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        instance = db.execute("SELECT status, power_state FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
    assert operation["status"] == "running"
    assert instance["status"] == "ready"
    assert instance["power_state"] == "stopped"


def test_power_start_runs_ubuntu_dhcp_identity_before_succeeding(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    operation_id = "power-identity-operation"
    instance_id = "power-identity-instance"
    spec = {
        "image": "ubuntu-2404",
        "network_profile": "lab_dhcp",
        "network_mode": "dhcp",
        "started": False,
    }
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, instance_id, secret_path, created_at)
            VALUES ('plan-identity', 'provision', 'succeeded', '{}', ?, NULL, 'now')
            """,
            (instance_id,),
        )
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, stack, created_at)
            VALUES (?, 'power', 'power', 'running', ?, ?, 'mng01', 'now')
            """,
            (
                operation_id,
                json.dumps({"command": "power", "power_action": "start", "instance_id": instance_id}),
                instance_id,
            ),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, ?, 'identity-test', 'ubuntu-2404', 40000, 'mng01', 'ready', 'stopped', ?, 'now', 'now')
            """,
            (instance_id, "plan-identity", json.dumps(spec)),
        )

    class FakePower:
        def __init__(self):
            self.power_calls = []

        def power_state(self, *_args):
            return {"power_state": "stopped"}

        def power(self, node, vmid, name, action):
            self.power_calls.append((node, vmid, name, action))
            assert (node, vmid, name, action) == ("mng01", 40000, "identity-test", "start")
            return {"action": action, "power_state": "running"}

    calls = []
    preboot_checks = []
    monkeypatch.setattr(
        "worker.main.ensure_guest_identity_for_operation",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "worker.main.expected_preboot_for_spec",
        lambda *_args, **_kwargs: {"instance": {"dns_servers": []}},
    )
    monkeypatch.setattr(
        "worker.main.verify_preboot_configuration",
        lambda _operation_id, client, **kwargs: (
            preboot_checks.append(kwargs.get("require_stopped"))
            or client.power_state()
        ),
    )
    client = FakePower()
    execute_power(
        "worker-1",
        {
            "operation_id": operation_id,
            "payload_json": json.dumps(
                {"command": "power", "power_action": "start", "instance_id": instance_id}
            ),
        },
        client,
    )

    assert len(calls) == 1
    assert calls[0][1]["instance_id"] == instance_id
    assert calls[0][1]["vmid"] == 40000
    assert calls[0][1]["spec"] == spec
    assert preboot_checks == [False, True]
    assert client.power_calls == [("mng01", 40000, "identity-test", "start")]


def test_power_start_is_idempotent_when_vm_is_already_running(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    operation_id = "power-already-running-operation"
    instance_id = "power-already-running-instance"
    spec = {"image": "ubuntu-2404", "network_profile": "lab_dhcp", "started": False}
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, instance_id, created_at)
            VALUES ('already-running-source', 'provision', 'succeeded', '{}', ?, 'now')
            """,
            (instance_id,),
        )
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, instance_id, stack, created_at)
            VALUES (?, 'power', 'running', ?, ?, 'mng01', 'now')
            """,
            (
                operation_id,
                json.dumps({"command": "power", "power_action": "start", "instance_id": instance_id}),
                instance_id,
            ),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status,
                                  power_state, spec_json, created_at, updated_at)
            VALUES (?, 'already-running-source', 'already-running-vm', 'ubuntu-2404',
                    40000, 'mng01', 'ready', 'running', ?, 'now', 'now')
            """,
            (instance_id, json.dumps(spec)),
        )

    class AlreadyRunning:
        def power_state(self, *_args):
            return {"power_state": "running"}

        def ensure_cloudinit_dns(self, *_args):
            pytest.fail("start idempotente no debe modificar cloud-init de una VM activa")

        def power(self, *_args):
            pytest.fail("start no debe enviar otra mutación si la VM ya está running")

    checks = []
    monkeypatch.setattr(
        "worker.main.expected_preboot_for_spec",
        lambda *_args, **_kwargs: {"instance": {"dns_servers": ["192.0.2.53"]}},
    )
    monkeypatch.setattr(
        "worker.main.verify_preboot_configuration",
        lambda _operation_id, client, **kwargs: (
            checks.append(kwargs["require_stopped"]) or client.power_state()
        ),
    )
    monkeypatch.setattr("worker.main.guest_identity_previously_verified", lambda _instance_id: True)

    execute_power(
        "worker-1",
        {
            "operation_id": operation_id,
            "payload_json": json.dumps(
                {"command": "power", "power_action": "start", "instance_id": instance_id}
            ),
        },
        AlreadyRunning(),
    )

    with database() as db:
        operation = db.execute(
            "SELECT status, output FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        instance = db.execute(
            "SELECT status, power_state FROM instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
    assert checks == [False]
    assert operation["status"] == "succeeded"
    assert json.loads(operation["output"])["already"] is True
    assert (instance["status"], instance["power_state"]) == ("ready", "running")


def test_power_start_does_not_mutate_if_last_preboot_read_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    operation_id = "power-last-gate-operation"
    instance_id = "power-last-gate-instance"
    spec = {"image": "ubuntu-2404", "network_profile": "lab_dhcp", "started": False}
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, instance_id, created_at)
            VALUES ('power-last-gate-source', 'provision', 'succeeded', '{}', ?, 'now')
            """,
            (instance_id,),
        )
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, stack, created_at)
            VALUES (?, 'power', 'power', 'running', ?, ?, 'mng01', 'now')
            """,
            (operation_id, json.dumps({"power_action": "start", "instance_id": instance_id}), instance_id),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, 'power-last-gate-source', 'last-gate-test', 'ubuntu-2404', 40009, 'mng01', 'ready', 'stopped', ?, 'now', 'now')
            """,
            (instance_id, json.dumps(spec)),
        )

    class FakePower:
        def __init__(self):
            self.power_calls = []

        def power_state(self, *_args):
            return {"power_state": "stopped"}

        def power(self, *_args):
            self.power_calls.append("power")
            pytest.fail("no se inicia si la última lectura de configuración falla")

    checks = []

    def verify(_operation_id, client, **_kwargs):
        checks.append(True)
        if len(checks) == 2:
            raise ProxmoxError("PREBOOT_VALIDATION_FAILED:memory_mb")
        return client.power_state()

    monkeypatch.setattr(
        "worker.main.expected_preboot_for_spec",
        lambda *_args, **_kwargs: {"instance": {"dns_servers": []}},
    )
    monkeypatch.setattr("worker.main.verify_preboot_configuration", verify)
    client = FakePower()
    with pytest.raises(ProxmoxError, match="PREBOOT_VALIDATION_FAILED:memory_mb"):
        execute_power(
            "worker-1",
            {
                "operation_id": operation_id,
                "payload_json": json.dumps({"power_action": "start", "instance_id": instance_id}),
            },
            client,
        )
    assert len(checks) == 2
    assert client.power_calls == []


def test_instance_password_presence_comes_from_original_operation_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, secret_path, created_at)
            VALUES ('credential-source', 'provision', 'succeeded', '{}', '/private/one-shot-secret', 'now')
            """
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, status, spec_json, created_at, updated_at)
            VALUES ('credential-instance', 'credential-source', 'credential-test', 'ubuntu-2404', 'ready', '{}', 'now', 'now')
            """
        )

    assert instance_password_was_provisioned("credential-instance") is True


def test_power_reboot_reuses_audited_guest_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    instance_id = "power-reboot-verified-instance"
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, instance_id, created_at)
            VALUES ('power-reboot-verified-provision', 'provision', 'succeeded', '{}', ?, 'now')
            """,
            (instance_id,),
        )
        db.execute(
            """
            INSERT INTO events(operation_id, kind, status, occurred_at)
            VALUES ('power-reboot-verified-provision', 'guest.identity.verified', 'succeeded', 'now')
            """
        )
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, stack, created_at)
            VALUES ('power-reboot-verified-action', 'power', 'power', 'running', ?, ?, 'mng01', 'now')
            """,
            (json.dumps({"command": "power", "power_action": "reboot", "instance_id": instance_id}), instance_id),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, 'power-reboot-verified-provision', 'power-reboot-verified', 'ubuntu-2404', 40000, 'mng01', 'ready', 'running', '{}', 'now', 'now')
            """,
            (instance_id,),
        )

    class FakePower:
        def power_state(self, *_args):
            return {"power_state": "running"}

        def power(self, *_args):
            return {"action": "reboot", "power_state": "running"}

    monkeypatch.setattr(
        "worker.main.ensure_guest_identity_for_operation",
        lambda *_args, **_kwargs: pytest.fail("la identidad verificada no debe depender del Guest Agent tras reboot"),
    )
    monkeypatch.setattr(
        "worker.main.expected_preboot_for_spec",
        lambda *_args, **_kwargs: {"instance": {"dns_servers": []}},
    )
    monkeypatch.setattr(
        "worker.main.verify_preboot_configuration",
        lambda _operation_id, client, **_kwargs: client.power_state(),
    )
    execute_power(
        "worker-1",
        {
            "operation_id": "power-reboot-verified-action",
            "payload_json": json.dumps(
                {"command": "power", "power_action": "reboot", "instance_id": instance_id}
            ),
        },
        FakePower(),
    )
    assert guest_identity_previously_verified(instance_id)
    with database() as db:
        operation = db.execute(
            "SELECT status FROM operations WHERE operation_id='power-reboot-verified-action'"
        ).fetchone()
    assert operation["status"] == "succeeded"


def test_reconcile_closes_reboot_identity_agent_outage_without_rebooting_again(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    instance_id = "reconcile-reboot-verified-instance"
    source_id = "reconcile-reboot-verified-source"
    recovery_id = "reconcile-reboot-verified-operation"
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, vmid, stack, created_at, error)
            VALUES (?, 'power', 'power', 'uncertain', ?, ?, 40001, 'mng01', 'now', 'GUEST_IDENTITY_AGENT_UNAVAILABLE')
            """,
            (source_id, json.dumps({"power_action": "reboot", "instance_id": instance_id}), instance_id),
        )
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, stack, created_at)
            VALUES (?, 'reconcile', 'reconcile', 'running', ?, ?, 'mng01', 'now')
            """,
            (recovery_id, json.dumps({"instance_id": instance_id, "source_operation_id": source_id}), instance_id),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, ?, 'reconcile-reboot-verified', 'ubuntu-2404', 40001, 'mng01', 'uncertain', 'unknown', '{}', 'now', 'now')
            """,
            (instance_id, source_id),
        )
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, instance_id, created_at)
            VALUES ('reconcile-reboot-verified-provision', 'provision', 'succeeded', '{}', ?, 'now')
            """,
            (instance_id,),
        )
        db.execute(
            """
            INSERT INTO events(operation_id, kind, status, occurred_at)
            VALUES ('reconcile-reboot-verified-provision', 'guest.identity.verified', 'succeeded', 'now')
            """
        )

    class ObservedRunning:
        def set_heartbeat(self, _callback):
            pass

        def power_state(self, node, vmid, name):
            assert (node, vmid, name) == ('mng01', 40001, 'reconcile-reboot-verified')
            return {"power_state": "running", "qmp_status": "running", "uptime": 90}

        def power(self, *_args):
            pytest.fail("la reconciliación no debe repetir el reboot")

    monkeypatch.setattr(
        "worker.main.expected_preboot_for_spec",
        lambda *_args, **_kwargs: {"instance": {"dns_servers": []}},
    )
    monkeypatch.setattr("worker.main.instance_password_was_provisioned", lambda _instance_id: False)
    monkeypatch.setattr(
        "worker.main.verify_preboot_configuration",
        lambda _operation_id, client, **_kwargs: client.power_state(
            "mng01", 40001, "reconcile-reboot-verified"
        ),
    )

    execute_reconcile(
        "worker-1",
        {
            "operation_id": recovery_id,
            "payload_json": json.dumps(
                {"command": "reconcile", "instance_id": instance_id, "source_operation_id": source_id}
            ),
        },
        ObservedRunning(),
    )
    with database() as db:
        source = db.execute("SELECT status FROM operations WHERE operation_id=?", (source_id,)).fetchone()
        recovery = db.execute("SELECT status FROM operations WHERE operation_id=?", (recovery_id,)).fetchone()
        instance = db.execute("SELECT status, power_state FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
    assert source["status"] == "succeeded"
    assert recovery["status"] == "succeeded"
    assert (instance["status"], instance["power_state"]) == ("ready", "running")


def test_reconcile_starts_recovered_provision_vm_before_guest_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    instance_id = "reconcile-stopped-provision-instance"
    source_id = "reconcile-stopped-provision-source"
    recovery_id = "reconcile-stopped-provision-operation"
    spec = {"image": "ubuntu-2404", "started": True}
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, vmid, stack, created_at)
            VALUES (?, 'provision', 'provision', 'uncertain', ?, ?, 40002, 'mng01', 'now')
            """,
            (source_id, json.dumps({"command": "provision", "instance_id": instance_id, "spec": spec}), instance_id),
        )
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, stack, created_at)
            VALUES (?, 'reconcile', 'reconcile', 'running', ?, ?, 'mng01', 'now')
            """,
            (recovery_id, json.dumps({"command": "reconcile", "instance_id": instance_id, "source_operation_id": source_id}), instance_id),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, ?, 'reconcile-stopped-provision', 'ubuntu-2404', 40002, 'mng01', 'uncertain', 'unknown', ?, 'now', 'now')
            """,
            (instance_id, source_id, json.dumps(spec)),
        )

    events = []

    class StoppedVm:
        current_state = "stopped"

        def set_heartbeat(self, _callback):
            pass

        def inventory(self, *, uncertain=False, timeout=None, attempts=None):
            return [{"vmid": 40002, "name": "reconcile-stopped-provision", "node": "mng01"}]

        def power_state(self, node, vmid, name):
            assert (node, vmid, name) == ("mng01", 40002, "reconcile-stopped-provision")
            return {"power_state": self.current_state}

        def power(self, node, vmid, name, action):
            assert (node, vmid, name, action) == ("mng01", 40002, "reconcile-stopped-provision", "start")
            events.append("power.start")
            self.current_state = "running"
            return {"action": action, "power_state": self.current_state}

    monkeypatch.setattr(
        "worker.main.ensure_guest_identity_for_operation",
        lambda *_args, **_kwargs: events.append("guest.identity"),
    )
    monkeypatch.setattr(
        "worker.main.operation_preboot_expectation",
        lambda *_args, **_kwargs: {"instance": {"dns_servers": []}},
    )
    monkeypatch.setattr(
        "worker.main.verify_preboot_configuration",
        lambda _operation_id, client, **_kwargs: client.power_state(
            "mng01", 40002, "reconcile-stopped-provision"
        ),
    )
    execute_reconcile(
        "worker-1",
        {
            "operation_id": recovery_id,
            "payload_json": json.dumps(
                {"command": "reconcile", "instance_id": instance_id, "source_operation_id": source_id}
            ),
        },
        StoppedVm(),
    )

    with database() as db:
        source = db.execute("SELECT status FROM operations WHERE operation_id=?", (source_id,)).fetchone()
        recovery = db.execute("SELECT status FROM operations WHERE operation_id=?", (recovery_id,)).fetchone()
        instance = db.execute("SELECT status, power_state FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
        kinds = [row["kind"] for row in db.execute("SELECT kind FROM events WHERE operation_id=? ORDER BY event_id", (recovery_id,))]
    assert events == ["power.start", "guest.identity"]
    assert source["status"] == "succeeded"
    assert recovery["status"] == "succeeded"
    assert (instance["status"], instance["power_state"]) == ("ready", "running")
    assert "provision.power.start.started" in kinds
    assert "provision.power.start.completed" in kinds


@pytest.mark.parametrize(
    "failure_code",
    ["SERVICE_BOOTSTRAP_GUEST_FAILED", "STATIC_GATEWAY_UNREACHABLE"],
)
def test_reconcile_marks_present_guest_failure_terminal_without_releasing_resources(
    tmp_path, monkeypatch, failure_code
):
    from worker.guest_identity import GuestIdentityError

    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    instance_id = "reconcile-present-guest-failure-instance"
    source_id = "reconcile-present-guest-failure-source"
    recovery_id = "reconcile-present-guest-failure-operation"
    name = "prueba-service-nextcloud-almalinux-9-kvm-1"
    spec = {
        "name": name,
        "image": "almalinux-9",
        "network_profile": "prod_static",
        "started": True,
    }
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(
                operation_id, command, action, status, payload_json, instance_id,
                vmid, stack, created_at
            ) VALUES (?, 'provision', 'provision', 'uncertain', ?, ?, 40044,
                      'mng01', 'now')
            """,
            (source_id, json.dumps({"command": "provision", "instance_id": instance_id, "spec": spec}), instance_id),
        )
        db.execute(
            """
            INSERT INTO events(operation_id, kind, status, occurred_at)
            VALUES (?, 'kvm.preboot.verified', 'succeeded', 'now')
            """,
            (source_id,),
        )
        db.execute(
            """
            INSERT INTO operations(
                operation_id, command, action, status, payload_json, instance_id,
                stack, created_at
            ) VALUES (?, 'reconcile', 'reconcile', 'running', ?, ?, 'mng01', 'now')
            """,
            (recovery_id, json.dumps({"instance_id": instance_id, "source_operation_id": source_id}), instance_id),
        )
        db.execute(
            """
            INSERT INTO instances(
                instance_id, operation_id, name, image, vmid, node, status,
                power_state, spec_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'almalinux-9', 40044, 'mng01', 'uncertain',
                      'unknown', ?, 'now', 'now')
            """,
            (instance_id, source_id, name, json.dumps(spec)),
        )

    class PresentRunningVm:
        def set_heartbeat(self, _callback):
            pass

        def inventory(self, *, uncertain=False, timeout=None, attempts=None):
            return [{"vmid": 40044, "name": name, "node": "mng01"}]

        def power_state(self, node, vmid, requested_name):
            assert (node, vmid, requested_name) == ("mng01", 40044, name)
            return {"power_state": "running"}

    def fail_release(*_args, **_kwargs):
        pytest.fail("una VM presente debe retener VMID e IP hasta su eliminación confirmada")

    def fail_identity(*_args, **_kwargs):
        raise GuestIdentityError(failure_code)

    monkeypatch.setattr("worker.main.ensure_guest_identity_for_operation", fail_identity)
    monkeypatch.setattr(
        "worker.main.operation_preboot_expectation",
        lambda *_args, **_kwargs: {"instance": {"dns_servers": []}},
    )
    monkeypatch.setattr(
        "worker.main.verify_preboot_configuration",
        lambda _operation_id, client, **_kwargs: client.power_state("mng01", 40044, name),
    )
    monkeypatch.setattr("worker.main._release_static_allocation", fail_release)
    monkeypatch.setattr("worker.main.release", fail_release)

    execute_reconcile(
        "worker-1",
        {
            "operation_id": recovery_id,
            "payload_json": json.dumps(
                {"command": "reconcile", "instance_id": instance_id, "source_operation_id": source_id}
            ),
        },
        PresentRunningVm(),
    )

    with database() as db:
        source = db.execute(
            "SELECT status, phase, error FROM operations WHERE operation_id=?", (source_id,)
        ).fetchone()
        recovery = db.execute(
            "SELECT status, error FROM operations WHERE operation_id=?", (recovery_id,)
        ).fetchone()
        instance = db.execute(
            "SELECT status, power_state, vmid FROM instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
        events = [
            row["kind"]
            for row in db.execute(
                "SELECT kind FROM events WHERE operation_id=? ORDER BY event_id",
                (recovery_id,),
            )
        ]

    assert (source["status"], source["phase"]) == ("failed", "guest_identity_failed")
    assert source["error"] == failure_code
    assert (recovery["status"], recovery["error"]) == (
        "failed",
        "RECONCILIATION_GUEST_IDENTITY_FAILED",
    )
    assert (instance["status"], instance["power_state"], instance["vmid"]) == (
        "failed",
        "running",
        40044,
    )
    assert "recovery.completed" in events


def test_ensure_vm_running_if_requested_fails_closed_for_unknown_power_state(monkeypatch):
    from worker import main as worker_main

    class UnknownPower:
        def power_state(self, *_args):
            return {"power_state": "unknown"}

        def power(self, *_args):
            pytest.fail("no debe iniciar cuando Proxmox no confirma stopped")

    def verify(_operation_id, client, **_kwargs):
        if client.power_state() != {"power_state": "stopped"}:
            raise ProxmoxError("PROVISION_POWER_STATE_UNCONFIRMED", uncertain=True)

    monkeypatch.setattr(worker_main, "verify_preboot_configuration", verify)
    monkeypatch.setattr(worker_main, "preboot_previously_verified", lambda _operation_id: False)
    with pytest.raises(ProxmoxError, match="PROVISION_POWER_STATE_UNCONFIRMED") as error:
        ensure_vm_running_if_requested(
            "worker-1",
            "operation-unknown-power",
            UnknownPower(),
            node="mng01",
            vmid=40003,
            name="unknown-power",
            started=True,
            expected={"instance": {"vm_name": "unknown-power", "dns_servers": []}},
        )
    assert error.value.uncertain


def test_ensure_vm_running_sets_and_verifies_cloudinit_dns_before_requested_start(
    monkeypatch,
):
    from worker import main as worker_main

    events = []
    monkeypatch.setattr(worker_main, "emit", lambda *args: events.append(args))

    class StaticVM:
        def __init__(self):
            self.calls = []

        def ensure_cloudinit_dns(self, node, vmid, name, servers):
            self.calls.append(("dns", node, vmid, name, servers))
            return {"nameserver": "1.1.1.1 8.8.8.8"}

        def power_state(self, *_args):
            self.calls.append(("power_state",))
            return {"power_state": "stopped"}

        def power(self, node, vmid, name, action):
            self.calls.append(("power", action))
            return {"power_state": "running"}

    def verify(_operation_id, client, **kwargs):
        client.calls.append(("preboot_gate", kwargs.get("record", True)))
        return client.power_state()

    monkeypatch.setattr(worker_main, "verify_preboot_configuration", verify)
    monkeypatch.setattr(worker_main, "preboot_previously_verified", lambda _operation_id: False)
    monkeypatch.setattr(worker_main, "heartbeat", lambda *_args, **_kwargs: None)

    client = StaticVM()
    started = ensure_vm_running_if_requested(
        "worker-1",
        "static-dns-start",
        client,
        node="mng01",
        vmid=40003,
        name="static-dns-start",
        started=True,
        expected={
            "instance": {
                "vm_name": "static-dns-start",
                "started": False,
                "dns_servers": ["1.1.1.1", "8.8.8.8"],
            }
        },
    )
    assert client.calls == [
        ("dns", "mng01", 40003, "static-dns-start", ["1.1.1.1", "8.8.8.8"]),
        ("preboot_gate", True),
        ("power_state",),
        ("preboot_gate", False),
        ("power_state",),
        ("power", "start"),
    ]
    assert started == {"power_state": "running"}
    assert events[0][1:3] == ("cloudinit.dns.verified", "succeeded")


def test_provision_does_not_start_if_final_stopped_configuration_read_fails(monkeypatch):
    from worker import main as worker_main

    class ChangedBeforeStart:
        def __init__(self):
            self.gate_calls = 0
            self.power_calls = []

        def power_state(self, *_args):
            return {"power_state": "stopped"}

        def power(self, *_args):
            self.power_calls.append("power")
            pytest.fail("la VM debe quedar detenida ante cualquier discrepancia final")

    client = ChangedBeforeStart()

    def verify(_operation_id, _client, **_kwargs):
        client.gate_calls += 1
        if client.gate_calls == 2:
            raise ProxmoxError("PREBOOT_VALIDATION_FAILED:network_gateway")
        return {"power_state": "stopped"}

    monkeypatch.setattr(worker_main, "verify_preboot_configuration", verify)
    monkeypatch.setattr(worker_main, "preboot_previously_verified", lambda _operation_id: False)
    monkeypatch.setattr(worker_main, "heartbeat", lambda *_args, **_kwargs: None)
    with pytest.raises(ProxmoxError, match="PREBOOT_VALIDATION_FAILED:network_gateway"):
        ensure_vm_running_if_requested(
            "worker-1",
            "preboot-last-read",
            client,
            node="mng01",
            vmid=40004,
            name="preboot-last-read",
            started=True,
            expected={"instance": {"vm_name": "preboot-last-read", "dns_servers": []}},
        )
    assert client.gate_calls == 2
    assert client.power_calls == []


def test_proxmox_ensure_cloudinit_dns_updates_only_verified_vm_config(monkeypatch):
    client = ProxmoxClient.__new__(ProxmoxClient)
    config = {
        "name": "static-dns-test",
        "template": 0,
        "net0": "virtio=aa:bb:cc:dd:ee:ff,bridge=vmbr3",
        "nameserver": "",
    }
    writes = []
    monkeypatch.setattr(client, "vm_config", lambda *_: dict(config))
    monkeypatch.setattr(client, "power_state", lambda *_: {"power_state": "stopped"})

    def put(path, parameters):
        writes.append((path, parameters))
        config.update(parameters)

    monkeypatch.setattr(client, "put", put)
    result = client.ensure_cloudinit_dns(
        "mng01", 40003, "static-dns-test", ["1.1.1.1", "8.8.8.8"]
    )
    assert result == {"nameserver": "1.1.1.1 8.8.8.8"}
    assert writes == [
        (
            "/nodes/mng01/qemu/40003/config",
            {"nameserver": "1.1.1.1 8.8.8.8"},
        )
    ]


def test_proxmox_ensure_cloudinit_dns_rejects_identity_mismatch_without_write(
    monkeypatch,
):
    client = ProxmoxClient.__new__(ProxmoxClient)
    writes = []
    monkeypatch.setattr(
        client,
        "vm_config",
        lambda *_: {
            "name": "different-vm",
            "template": 0,
            "net0": "virtio=aa:bb:cc:dd:ee:ff,bridge=vmbr3",
        },
    )
    monkeypatch.setattr(client, "put", lambda *args: writes.append(args))
    with pytest.raises(ProxmoxError, match="CLOUDINIT_DNS_IDENTITY_MISMATCH"):
        client.ensure_cloudinit_dns(
            "mng01", 40003, "expected-vm", ["1.1.1.1", "8.8.8.8"]
        )
    assert writes == []


def test_execute_starts_physically_recovered_vm_before_guest_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    initialize()
    operation_id = "execute-recovered-stopped-provision"
    instance_id = "execute-recovered-stopped-instance"
    spec = {
        "name": "execute-recovered-stopped",
        "image": "ubuntu-2404",
        "vcpus": 2,
        "memory_mb": 2048,
        "disk_gb": 50,
        "network_mode": "dhcp",
        "network_profile": "lab_dhcp",
        "started": True,
        "tags": ["e2e"],
    }
    payload = {
        "command": "provision",
        "action": "provision",
        "stack": "mng01",
        "instance_id": instance_id,
        "spec": spec,
    }
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, stack, created_at)
            VALUES (?, 'provision', 'provision', 'running', ?, ?, 'mng01', 'now')
            """,
            (operation_id, json.dumps(payload), instance_id),
        )
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, vmid, node, status, power_state, spec_json, created_at, updated_at)
            VALUES (?, ?, ?, 'ubuntu-2404', NULL, 'mng01', 'provisioning', 'unknown', ?, 'now', 'now')
            """,
            (instance_id, operation_id, spec["name"], json.dumps(spec)),
        )

    actions = []

    class StoppedVm:
        current_state = "stopped"

        def set_heartbeat(self, _callback):
            pass

        def inventory(self, *, uncertain=False, timeout=None, attempts=None):
            return [{"vmid": 40010, "name": spec["name"], "node": "mng01"}]

        def power_state(self, node, vmid, name):
            assert (node, vmid, name) == ("mng01", 40010, spec["name"])
            return {"power_state": self.current_state}

        def power(self, node, vmid, name, action):
            assert (node, vmid, name, action) == ("mng01", 40010, spec["name"], "start")
            actions.append("power.start")
            self.current_state = "running"
            return {"action": action, "power_state": self.current_state}

    client = StoppedVm()
    commands_run = iter([(0, "plan ready", ""), (1, "", "temporary failure in name resolution")])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("worker.main.load_environment", lambda: None)
    monkeypatch.setattr("worker.main.ProxmoxClient", lambda: client)
    monkeypatch.setattr("worker.main.validate_pool", lambda _client: "iacp-lab")
    monkeypatch.setattr(
        "worker.main.observe",
        lambda image_id, *_args: {"image_id": image_id, "network_model": "virtio"},
    )
    monkeypatch.setattr("worker.main.cluster_nodes", lambda: ["mng01"])
    monkeypatch.setattr("worker.main.select_target_node", lambda _client: "mng01")
    monkeypatch.setattr("worker.main.allocate", lambda *_args, **_kwargs: 40010)
    monkeypatch.setattr("worker.main._ensure_static_allocation", lambda **kwargs: dict(kwargs["payload"]["spec"]))
    monkeypatch.setattr(
        "worker.main.golden_variables",
        lambda spec, vmid, *_args, **_kwargs: {
            "instance": {
                "vm_name": spec["name"],
                "vm_id": vmid,
                "node_name": "mng01",
                "image": spec["image"],
                "started": False,
                "dns_servers": [],
            }
        },
    )
    monkeypatch.setattr("worker.main.read_secret", lambda _path: None)
    monkeypatch.setattr("worker.main.create_workspace", lambda *_args: (str(workspace), str(workspace)))
    monkeypatch.setattr("worker.main.write_variables", lambda *_args: "variables.json")
    monkeypatch.setattr("worker.main.terraform_commands", lambda *_args: [])
    monkeypatch.setattr("worker.main.assert_stopped_plan", lambda *_args: None)
    monkeypatch.setattr(
        "worker.main.verify_preboot_configuration",
        lambda _operation_id, client, **_kwargs: client.power_state("mng01", 40010, spec["name"]),
    )
    monkeypatch.setattr("worker.main.secret_path_for_command", lambda *_args: None)
    monkeypatch.setattr("worker.main.run_commands", lambda *_args, **_kwargs: next(commands_run))
    monkeypatch.setattr("worker.main.ensure_guest_identity_for_operation", lambda *_args, **_kwargs: actions.append("guest.identity"))
    monkeypatch.setattr("worker.main.delete_secret", lambda _path: None)

    execute(
        "worker-1",
        {
            "operation_id": operation_id,
            "stack": "mng01",
            "instance_id": instance_id,
            "payload_json": json.dumps(payload),
        },
    )

    with database() as db:
        operation = db.execute("SELECT status FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        instance = db.execute("SELECT status, power_state, vmid FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
    assert actions == ["power.start", "guest.identity"]
    assert operation["status"] == "succeeded"
    assert (instance["status"], instance["power_state"], instance["vmid"]) == ("ready", "running", 40010)


def test_heartbeat_refreshes_operation_phase_and_worker_age(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
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
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
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
    recovery_payload = json.loads(recovery["payload_json"])
    assert recovery_payload["source_operation_id"] == "stale-operation"
    assert recovery_payload["recovery_attempt"] == 1
    assert instance["status"] == "uncertain"
    assert instance["power_state"] == "unknown"
    assert recover_stale_operations() == 0


def test_stale_reconcile_queues_bounded_retry_for_original_operation(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_OPERATION_STALE_SECONDS", "30")
    initialize()
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    instance_id = "stale-reconcile-instance"
    with database() as db:
        db.execute(
            """INSERT INTO operations(
              operation_id, command, status, payload_json, instance_id, vmid,
              stack, created_at, last_heartbeat
            ) VALUES ('stale-reconcile-source', 'provision', 'uncertain', '{}', ?,
                      40000, 'mng01', ?, ?)""",
            (instance_id, old, old),
        )
        db.execute(
            """INSERT INTO operations(
              operation_id, command, action, status, payload_json, instance_id,
              vmid, stack, created_at, started_at, last_heartbeat, phase
            ) VALUES ('stale-reconcile-attempt-1', 'reconcile', 'reconcile', 'running',
                     ?, ?, 40000, 'mng01', ?, ?, ?, 'reconcile')""",
            (
                json.dumps({"command": "reconcile", "source_operation_id": "stale-reconcile-source"}),
                instance_id,
                old,
                old,
                old,
            ),
        )
        db.execute(
            """INSERT INTO instances(
              instance_id, operation_id, name, image, vmid, node, status,
              power_state, spec_json, created_at, updated_at
            ) VALUES (?, 'stale-reconcile-source', 'stale-reconcile-vm', 'ubuntu-2404',
                      40000, 'mng01', 'uncertain', 'unknown', '{}', ?, ?)""",
            (instance_id, old, old),
        )

    assert recover_stale_operations() == 1

    with database() as db:
        attempts = db.execute(
            "SELECT operation_id, status, payload_json FROM operations WHERE command='reconcile' ORDER BY created_at, operation_id"
        ).fetchall()
    assert len(attempts) == 2
    payload = json.loads(attempts[1]["payload_json"])
    assert attempts[0]["status"] == "uncertain"
    assert attempts[1]["status"] == "pending"
    assert payload["source_operation_id"] == "stale-reconcile-source"
    assert payload["recovery_attempt"] == 2


def test_stale_reconcile_stops_after_three_automatic_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_OPERATION_STALE_SECONDS", "30")
    initialize()
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    instance_id = "stale-reconcile-limit-instance"
    with database() as db:
        db.execute(
            """INSERT INTO operations(
              operation_id, command, status, payload_json, instance_id, vmid,
              stack, created_at, last_heartbeat
            ) VALUES ('stale-reconcile-limit-source', 'provision', 'uncertain', '{}', ?,
                      40000, 'mng01', ?, ?)""",
            (instance_id, old, old),
        )
        for attempt in range(1, 4):
            status = "running" if attempt == 3 else "failed"
            heartbeat_at = old if attempt == 3 else "now"
            db.execute(
                """INSERT INTO operations(
                  operation_id, command, action, status, payload_json, instance_id,
                  vmid, stack, created_at, started_at, last_heartbeat, phase
                ) VALUES (?, 'reconcile', 'reconcile', ?, ?, ?, 40000,
                          'mng01', ?, ?, ?, 'reconcile')""",
                (
                    f"stale-reconcile-limit-attempt-{attempt}",
                    status,
                    json.dumps({"command": "reconcile", "source_operation_id": "stale-reconcile-limit-source"}),
                    instance_id,
                    old,
                    old,
                    heartbeat_at,
                ),
            )
        db.execute(
            """INSERT INTO instances(
              instance_id, operation_id, name, image, vmid, node, status,
              power_state, spec_json, created_at, updated_at
            ) VALUES (?, 'stale-reconcile-limit-source', 'stale-reconcile-limit-vm',
                      'ubuntu-2404', 40000, 'mng01', 'uncertain', 'unknown', '{}', ?, ?)""",
            (instance_id, old, old),
        )

    assert recover_stale_operations() == 1

    with database() as db:
        attempts = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE command='reconcile'"
        ).fetchone()["count"]
        exhausted = db.execute(
            """SELECT 1 FROM events WHERE operation_id='stale-reconcile-limit-attempt-3'
            AND kind='recovery.retry_exhausted' AND status='uncertain'"""
        ).fetchone()
    assert attempts == 3
    assert exhausted is not None


def test_stale_worker_with_live_execution_lock_is_not_reconciled(tmp_path, monkeypatch):
    import worker.main as main

    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_WORKDATA_DIR", str(tmp_path / "workdata"))
    monkeypatch.setenv("IACP_OPERATION_STALE_SECONDS", "30")
    initialize()
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json, instance_id,
              vmid, stack, created_at, started_at, last_heartbeat, phase
            ) VALUES (?, 'provision', 'provision', 'running', ?, ?, 40001, 'mng01', ?, ?, ?, 'apply')
            """,
            (
                "locked-stale-operation",
                json.dumps({"command": "provision", "action": "provision", "instance_id": "locked-stale-instance"}),
                "locked-stale-instance",
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
            ) VALUES (?, ?, 'locked-stale-vm', 'ubuntu-2404', 40001, 'mng01', 'applying', 'unknown', '{}', ?, ?)
            """,
            ("locked-stale-instance", "locked-stale-operation", old, old),
        )

    with main.operation_execution_lock("locked-stale-operation"):
        assert main.recover_stale_operations() == 0
        with database() as db:
            source = db.execute(
                "SELECT status FROM operations WHERE operation_id=?",
                ("locked-stale-operation",),
            ).fetchone()
            recovery_count = db.execute(
                "SELECT COUNT(*) AS n FROM operations WHERE command='reconcile'"
            ).fetchone()["n"]
        assert source["status"] == "running"
        assert recovery_count == 0

    assert main.recover_stale_operations() == 1


def test_operation_output_is_bounded_and_redacts_secret(monkeypatch):
    monkeypatch.setenv("IACP_OPERATION_OUTPUT_MAX_CHARS", "4096")
    value = operation_output("root-secret\n" + ("x" * 10000), "root-secret")
    assert "root-secret" not in value
    assert "output truncated" in value
    assert len(value) <= 4096


@pytest.mark.no_database
def test_destroy_validation_password_is_ephemeral():
    first = destroy_validation_password()
    second = destroy_validation_password()
    assert first != second
    assert len(first) >= 64


def test_release_static_keeps_ip_when_vm_still_exists(monkeypatch):
    import worker.main as main
    allocation = {"allocation_id": "allocation-test", "marker": "iacp-instance=test"}
    updates, events = [], []
    monkeypatch.setattr(main, "_ip_allocation", lambda _: allocation)
    monkeypatch.setattr(main, "instance_identity", lambda _: {"vmid": 40003})
    monkeypatch.setattr(main, "ProxmoxClient", lambda: object())
    monkeypatch.setattr(main, "reconcile_physical", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(main, "NetBoxClient", lambda: pytest.fail("No debe liberar una IP con VM presente"))
    monkeypatch.setattr(main, "_mark_ip_allocation", lambda *args, **kwargs: updates.append((args, kwargs)))
    monkeypatch.setattr(main, "emit", lambda *args: events.append(args))
    assert main._release_static_allocation("instance-test", "operation-test") is False
    assert updates[0][0] == ("allocation-test", "release_pending")
    assert updates[0][1]["error"] == "IPAM_VM_STILL_PRESENT"
    assert events[0][1] == "ipam.release.pending"


def test_static_ipam_keeps_vmbr3_without_tag(monkeypatch):
    import worker.main as main
    from types import SimpleNamespace

    monkeypatch.setenv("IACP_PROD_STATIC_ENABLED", "1")
    monkeypatch.setenv("IACP_PROD_STATIC_BRIDGE", "vmbr3")
    monkeypatch.setattr(main, "netbox_configured", lambda: True)
    monkeypatch.setattr(main, "_ip_allocation", lambda _instance_id: None)
    monkeypatch.setattr(
        main,
        "_begin_ip_allocation",
        lambda **_kwargs: {
            "allocation_id": "allocation-test",
            "status": "allocating",
        },
    )
    monkeypatch.setattr(main, "_complete_ip_allocation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "emit", lambda *_: None)
    monkeypatch.setattr(main, "_update_operation_payload", lambda *_: None)
    monkeypatch.setattr(main, "_update_instance_spec", lambda *_: None)

    class NetBox:
        def allocate(self, **_kwargs):
            return SimpleNamespace(
                ip_id=123,
                address="190.0.231.8/24",
                gateway="190.0.231.1",
            )

    monkeypatch.setattr(main, "NetBoxClient", NetBox)
    result = main._ensure_static_allocation(
        worker_id="worker-test",
        operation_id="operation-test",
        instance_id="instance-test",
        vmid=40008,
        payload={"spec": {"name": "prueba-kvm-wan", "network_profile": "prod_static"}},
        source_operation={"commercial_context_json": "{}"},
    )
    assert result["network_mode"] == "static"
    assert result["ipv4_cidr"] == "190.0.231.8/24"
    assert result["vlan_id"] is None


def test_ipam_ledger_preserves_legacy_prefix_ids_and_allows_direct_reservations():
    from api.db import initialize
    from worker.main import _begin_ip_allocation, _complete_ip_allocation, _ip_allocation
    from worker.netbox import NetBoxAllocation

    initialize()
    with database() as db:
        db.execute("ALTER TABLE ip_allocations ALTER COLUMN prefix_id SET NOT NULL")
        db.execute(
            """
            INSERT INTO ip_allocations(
                allocation_id, instance_id, operation_id, profile, prefix_id,
                netbox_ip_id, address, marker, status, created_at
            ) VALUES (?, ?, ?, 'prod_static', 52, 91, ?, ?, 'allocated', ?)
            """,
            ("legacy-allocation", "legacy-vm", "legacy-op", "190.0.231.7/24",
             "iacp-instance=legacy-vm", "2026-01-01T00:00:00+00:00"),
        )
    initialize()
    legacy = _ip_allocation("legacy-vm")
    assert legacy["prefix_id"] == 52 and legacy["netbox_ip_id"] == 91

    intent = _begin_ip_allocation(
        instance_id="direct-vm", operation_id="direct-op", marker="iacp-instance=direct-vm"
    )
    _complete_ip_allocation(intent["allocation_id"], NetBoxAllocation(
        92, "190.0.231.8/24", "190.0.231.0/24", "190.0.231.1", "iacp-instance=direct-vm"
    ))
    stored = _ip_allocation("direct-vm")
    assert stored["prefix_id"] is None
    assert stored["netbox_ip_id"] == 92 and stored["marker"] == "iacp-instance=direct-vm"


def test_operation_spec_preserves_ipam_resolved_payload():
    from worker.main import operation_spec
    old = {"payload_json": json.dumps({"spec": {"network_profile": "prod_static", "ipv4_cidr": None}})}
    resolved = {"spec": {"network_profile": "prod_static", "ipv4_cidr": "190.0.231.2/24", "ipv4_gateway": "190.0.231.1"}}
    assert operation_spec(old, resolved) == resolved["spec"]


@pytest.mark.no_database
def test_reconcile_inventory_requests_fit_inside_deadline(monkeypatch):
    import worker.main as worker_main

    monkeypatch.setenv("IACP_RECONCILE_TIMEOUT", "5")
    monkeypatch.setenv("IACP_RECONCILE_BACKOFF", "2")
    clock = [0.0]
    monkeypatch.setattr(worker_main.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        worker_main.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    class Client:
        def __init__(self):
            self.calls = []

        def inventory(self, *, uncertain=False, timeout=None, attempts=None):
            self.calls.append((uncertain, timeout, attempts))
            raise ProxmoxError("PROXMOX_UNAVAILABLE", 503)

    client = Client()
    assert not worker_main.reconcile_physical(client, 40001, False)
    assert [call[1:] for call in client.calls] == [(5.0, 1), (3.0, 1), (1.0, 1)]
    assert all(call[0] for call in client.calls)
    assert clock[0] == 5.0


def test_reboot_recovery_requires_unique_matching_task_and_time(monkeypatch):
    from worker.proxmox import ProxmoxClient
    client=object.__new__(ProxmoxClient)
    client.token='test@pve!runtime=not-a-real-token'
    task='UPID:mng01:000001:000002:000003:qmreboot:40018:test@pve!runtime:'
    row={'node':'mng01','id':'40018','type':'qmreboot','user':'test@pve','tokenid':'runtime','status':'OK','starttime':100,'endtime':103,'upid':task}
    records=[row]
    calls=[]
    monkeypatch.setattr(client,'get',lambda *_:records)
    monkeypatch.setattr(client,'wait_task',lambda *args:calls.append(args))
    assert client.completed_power_task_during('mng01',40018,'reboot','1970-01-01T00:01:40+00:00','1970-01-01T00:01:45+00:00')==task
    assert len(calls)==1
    records.append(dict(row))
    assert client.completed_power_task_during('mng01',40018,'reboot','1970-01-01T00:01:40+00:00','1970-01-01T00:01:45+00:00') is None
    records[:]=[dict(row,user='someone@pve')]
    assert client.completed_power_task_during('mng01',40018,'reboot','1970-01-01T00:01:40+00:00','1970-01-01T00:01:45+00:00') is None
    records[:]=[row]
    assert client.completed_power_task_during('mng01',40018,'reboot','1970-01-01T00:02:00+00:00','1970-01-01T00:02:05+00:00') is None


@pytest.mark.no_database
def test_tarea_qmclone_exige_token_destino_y_ventana_exactos(monkeypatch):
    client = object.__new__(ProxmoxClient)
    client.token = "test@pve!runtime=ficticio"
    task = "UPID:mng01:000001:000002:000003:qmclone:29010:test@pve!runtime:"
    record = {
        "node": "mng01",
        "id": "29010",
        "type": "qmclone",
        "user": "test@pve",
        "tokenid": "runtime",
        "status": "OK",
        "starttime": 102,
        "endtime": 103,
        "upid": task,
    }
    calls = []

    def fake_get(path, **kwargs):
        calls.append(path)
        if "/tasks?" in path:
            return [record]
        if "/log?" in path:
            return [{"t": "creating a clone of VM 29010 with ID 40018"}]
        return {"status": "stopped", "exitstatus": "OK"}

    monkeypatch.setattr(client, "get", fake_get)
    args = (
        "mng01", 40018, 29010,
        "1970-01-01T00:01:40+00:00",
        "1970-01-01T00:01:45+00:00",
    )
    assert client.completed_clone_task_during(*args) is True
    assert len(calls) == 3

    monkeypatch.setattr(client, "get", lambda *_args, **_kwargs: [dict(record, user="otro@pve")])
    assert client.completed_clone_task_during(*args) is False

    def duplicate_get(path, **kwargs):
        if "/tasks?" in path:
            return [record, dict(record, upid=task.replace("000001", "000004"))]
        if "/log?" in path:
            return [{"t": "creating a clone of VM 29010 with ID 40018"}]
        return {"status": "stopped", "exitstatus": "OK"}

    monkeypatch.setattr(client, "get", duplicate_get)
    assert client.completed_clone_task_during(*args) is False


@pytest.mark.parametrize('released,expected', [(True, 'deleted'), (False, 'uncertain')])
def test_reconcile_missing_vm_preserves_later_deletion(monkeypatch, released, expected):
    from worker import main
    initialize()
    with database() as db:
        db.execute("INSERT INTO operations(operation_id,command,status,payload_json,created_at) VALUES ('old-source','provision','uncertain','{}','now')")
        db.execute("""INSERT INTO instances(instance_id,operation_id,name,image,vmid,node,status,power_state,spec_json,created_at,updated_at)
            VALUES ('deleted-instance','old-source','prueba-service-nextcloud','almalinux-9',40001,'mng01','deleted','unknown','{}','now','now')""")
    monkeypatch.setattr(main, '_release_static_allocation', lambda *_: released)
    monkeypatch.setattr(main, 'release', lambda *_args, **_kwargs: None)
    main.close_source_after_reconciliation(
        {'operation_id': 'old-source', 'command': 'provision'},
        'deleted-instance', 40001, 'mng01', {}, succeeded=False, detail='VM_ABSENT',
    )
    with database() as db:
        assert db.execute("SELECT status FROM instances WHERE instance_id='deleted-instance'").fetchone()['status'] == expected
        assert db.execute("SELECT status FROM operations WHERE operation_id='old-source'").fetchone()['status'] == ('failed' if released else 'uncertain')


def test_reconcile_missing_provision_deletes_root_password_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_WORKDATA_DIR", str(tmp_path / "workdata"))
    from api.secrets import write_secret
    from worker import main

    initialize()
    secret = write_secret("missing-provision", "test-only-password")
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, status, payload_json, instance_id,
              vmid, secret_path, created_at
            ) VALUES (
              'missing-provision', 'provision', 'uncertain', '{}',
              'missing-provision-instance', 40001, ?, 'now'
            )
            """,
            (str(secret),),
        )
        db.execute(
            """
            INSERT INTO instances(
              instance_id, operation_id, name, image, vmid, node, status,
              power_state, spec_json, created_at, updated_at
            ) VALUES (
              'missing-provision-instance', 'missing-provision', 'missing-provision',
              'ubuntu-2404', 40001, 'mng01', 'uncertain', 'unknown', '{}', 'now', 'now'
            )
            """
        )
    monkeypatch.setattr(main, "_release_static_allocation", lambda *_: True)
    monkeypatch.setattr(main, "release", lambda *_args, **_kwargs: None)

    main.close_source_after_reconciliation(
        {
            "operation_id": "missing-provision",
            "command": "provision",
            "secret_path": str(secret),
        },
        "missing-provision-instance",
        40001,
        "mng01",
        {},
        succeeded=False,
        detail="RECONCILIATION_PHYSICAL_RESOURCE_ABSENT",
    )

    assert not secret.exists()
    with database() as db:
        row = db.execute(
            "SELECT status, secret_path, vmid, error FROM operations WHERE operation_id='missing-provision'"
        ).fetchone()
        instance = db.execute(
            "SELECT status, vmid, power_state FROM instances WHERE instance_id='missing-provision-instance'"
        ).fetchone()
    assert row["status"] == "failed"
    assert row["secret_path"] is None
    assert row["vmid"] == 40001
    assert row["error"] == "RECONCILIATION_PHYSICAL_RESOURCE_ABSENT"
    assert (instance["status"], instance["vmid"], instance["power_state"]) == (
        "failed", None, "unknown"
    )


def test_reconcile_closes_deleted_power_intent_only_after_confirming_absence(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    from worker import main as worker_main

    initialize()
    source_payload = {
        "command": "power",
        "action": "power",
        "instance_id": "deleted-power-instance",
        "power_action": "reboot",
    }
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json,
              instance_id, stack, created_at
            ) VALUES (
              'old-power', 'power', 'power', 'uncertain', ?,
              'deleted-power-instance', 'mng01', '2026-01-01T00:00:00Z'
            )
            """,
            (json.dumps(source_payload),),
        )
        for index in (2, 3):
            payload = {**source_payload, "power_action": "start" if index == 2 else "reboot"}
            db.execute(
                """
                INSERT INTO operations(
                  operation_id, command, action, status, payload_json,
                  instance_id, stack, created_at
                ) VALUES (?, 'power', 'power', 'uncertain', ?,
                  'deleted-power-instance', 'mng01', ?)
                """,
                (
                    f"old-power-{index}",
                    json.dumps(payload),
                    f"2026-01-01T0{index}:00:00Z",
                ),
            )
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json,
              instance_id, stack, created_at, finished_at
            ) VALUES (
              'later-destroy', 'destroy', 'destroy', 'succeeded', '{}',
              'deleted-power-instance', 'mng01', '2026-01-02T00:00:00Z', '2026-01-02T00:00:01Z'
            )
            """
        )
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json,
              instance_id, stack, created_at
            ) VALUES (
              'recovery-power', 'reconcile', 'reconcile', 'running', ?,
              'deleted-power-instance', 'mng01', '2026-01-03T00:00:00Z'
            )
            """,
            (json.dumps({
                "command": "reconcile",
                "action": "reconcile",
                "instance_id": "deleted-power-instance",
                "source_operation_id": "old-power",
            }),),
        )
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json,
              instance_id, stack, created_at
            ) VALUES (
              'old-recovery-attempt', 'reconcile', 'reconcile', 'uncertain', ?,
              'deleted-power-instance', 'mng01', '2026-01-03T00:00:00Z'
            )
            """,
            (json.dumps({
                "command": "reconcile",
                "instance_id": "deleted-power-instance",
                "source_operation_id": "old-power-2",
            }),),
        )
        db.execute(
            """
            INSERT INTO instances(
              instance_id, operation_id, name, image, vmid, node, status,
              power_state, spec_json, created_at, updated_at
            ) VALUES (
              'deleted-power-instance', 'old-power', 'deleted-power', 'fedora-45',
              40239, 'mng01', 'deleted', 'unknown', '{}', '2026-01-01T00:00:00Z',
              '2026-01-02T00:00:01Z'
            )
            """
        )

    observations = []

    def confirm_absent(client, vmid, expected, **kwargs):
        observations.append((vmid, expected, kwargs.get("name"), kwargs.get("node")))
        return True

    monkeypatch.setattr(worker_main, "reconcile_physical", confirm_absent)
    monkeypatch.setattr(worker_main, "heartbeat", lambda *args, **kwargs: None)
    worker_main.execute_reconcile(
        "worker-test",
        {
            "operation_id": "recovery-power",
            "payload_json": json.dumps({
                "command": "reconcile",
                "instance_id": "deleted-power-instance",
                "source_operation_id": "old-power",
            }),
            "instance_id": "deleted-power-instance",
        },
        FakeProxmox(),
    )

    assert observations == [(40239, False, "deleted-power", "mng01")]
    with database() as db:
        sources = db.execute(
            "SELECT operation_id, status, error FROM operations WHERE instance_id='deleted-power-instance' AND command='power' ORDER BY operation_id"
        ).fetchall()
        recovery = db.execute(
            "SELECT status, error FROM operations WHERE operation_id='recovery-power'"
        ).fetchone()
        old_recovery = db.execute(
            "SELECT status, error FROM operations WHERE operation_id='old-recovery-attempt'"
        ).fetchone()
        instance = db.execute(
            "SELECT status, power_state FROM instances WHERE instance_id='deleted-power-instance'"
        ).fetchone()
        event_count = db.execute(
            "SELECT COUNT(*) AS count FROM events WHERE kind='operation.superseded_by_destroy' AND status='failed'"
        ).fetchone()["count"]
    assert len(sources) == 3
    assert all(row["status"] == "failed" for row in sources)
    assert all("eliminación confirmada" in row["error"] for row in sources)
    assert recovery["status"] == "succeeded" and recovery["error"] == ""
    assert old_recovery["status"] == "failed"
    assert (instance["status"], instance["power_state"]) == ("deleted", "unknown")
    assert event_count == 3


def test_reconcile_supersedes_old_power_and_recovery_after_later_verified_lifecycle(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    from worker import main as worker_main

    initialize()
    source_payload = {
        "command": "power",
        "action": "power",
        "instance_id": "active-power-instance",
        "power_action": "reboot",
    }
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json,
              instance_id, stack, created_at
            ) VALUES (
              'old-active-power', 'power', 'power', 'uncertain', ?,
              'active-power-instance', 'mng01', '2026-01-01T00:00:00Z'
            )
            """,
            (json.dumps(source_payload),),
        )
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json,
              instance_id, stack, created_at, finished_at
            ) VALUES (
              'confirmed-later-power', 'power', 'power', 'succeeded', ?,
              'active-power-instance', 'mng01', '2026-01-02T00:00:00Z',
              '2026-01-02T00:00:01Z'
            )
            """,
            (json.dumps(source_payload),),
        )
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json,
              instance_id, stack, created_at
            ) VALUES (
              'old-recovery-attempt', 'reconcile', 'reconcile', 'uncertain', ?,
              'active-power-instance', 'mng01', '2026-01-01T01:00:00Z'
            )
            """,
            (json.dumps({
                "command": "reconcile",
                "instance_id": "active-power-instance",
                "source_operation_id": "old-active-power",
            }),),
        )
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json,
              instance_id, stack, created_at
            ) VALUES (
              'current-recovery-attempt', 'reconcile', 'reconcile', 'running', ?,
              'active-power-instance', 'mng01', '2026-01-03T00:00:00Z'
            )
            """,
            (json.dumps({
                "command": "reconcile",
                "instance_id": "active-power-instance",
                "source_operation_id": "old-active-power",
            }),),
        )
        db.execute(
            """
            INSERT INTO instances(
              instance_id, operation_id, name, image, vmid, node, status,
              power_state, spec_json, created_at, updated_at
            ) VALUES (
              'active-power-instance', 'old-active-power', 'active-power',
              'ubuntu-2404', 40238, 'mng01', 'ready', 'running', '{}',
              '2026-01-01T00:00:00Z', '2026-01-02T00:00:01Z'
            )
            """
        )

    identity_calls = []
    monkeypatch.setattr(worker_main, "expected_preboot_for_spec", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        worker_main,
        "verify_preboot_configuration",
        lambda *args, **kwargs: {"power_state": "running"},
    )
    monkeypatch.setattr(
        worker_main,
        "ensure_guest_identity_for_operation",
        lambda *args, **kwargs: identity_calls.append(kwargs.get("started")),
    )
    monkeypatch.setattr(worker_main, "instance_password_was_provisioned", lambda *_: False)
    monkeypatch.setattr(worker_main, "heartbeat", lambda *args, **kwargs: None)
    worker_main.execute_reconcile(
        "worker-test",
        {
            "operation_id": "current-recovery-attempt",
            "payload_json": json.dumps({
                "command": "reconcile",
                "instance_id": "active-power-instance",
                "source_operation_id": "old-active-power",
            }),
            "instance_id": "active-power-instance",
        },
        FakeProxmox(),
    )

    assert identity_calls == [True]
    with database() as db:
        source = db.execute(
            "SELECT status, error FROM operations WHERE operation_id='old-active-power'"
        ).fetchone()
        old_recovery = db.execute(
            "SELECT status, error FROM operations WHERE operation_id='old-recovery-attempt'"
        ).fetchone()
        recovery = db.execute(
            "SELECT status, error FROM operations WHERE operation_id='current-recovery-attempt'"
        ).fetchone()
        instance = db.execute(
            "SELECT status, power_state FROM instances WHERE instance_id='active-power-instance'"
        ).fetchone()
        event = db.execute(
            "SELECT status FROM events WHERE operation_id='old-active-power' AND kind='operation.reconciled_by_later_lifecycle'"
        ).fetchone()
    assert source["status"] == "failed" and "posterior" in source["error"]
    assert old_recovery["status"] == "failed"
    assert recovery["status"] == "succeeded" and recovery["error"] == ""
    assert (instance["status"], instance["power_state"]) == ("ready", "running")
    assert event["status"] == "failed"
