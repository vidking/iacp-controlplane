from worker.allocation import allocate, release
from worker.golden import variables
from worker.main import local_variables


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
