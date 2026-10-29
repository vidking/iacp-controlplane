import json

import pytest

from api.db import database, initialize
from worker.allocation import allocate, release
from worker.golden import variables
from worker.main import execute_power, local_variables, terraform_commands
from worker.proxmox import credentials


class FakeProxmox:
    def __init__(self):
        self.items = []

    def inventory(self):
        return self.items


def test_allocation_reuses_released_vmid(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_DB_PATH", str(tmp_path / "iacp.sqlite"))
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40000")
    from api.db import initialize

    initialize()
    client = FakeProxmox()
    first = allocate("operation-a", client)
    release(first)
    second = allocate("operation-b", client)
    assert first == second == 40000


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
    assert instance["ipv4_cidr"] is None
    assert instance["boot_order"] == ["scsi0"]
    assert instance["ssh_public_keys"] == [key]


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


def test_power_operation_updates_instance_and_operation(tmp_path, monkeypatch):
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
