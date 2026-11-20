import shlex

import pytest

from scripts.prepare_golden_copies import (
    BASE_PACKAGES,
    CLOUD_INIT_SERVICES,
    COPIES,
    GOLDEN_HARDWARE,
    GUEST_BOOTSTRAP_CHECKS,
    PROTECTED_BASE_VMIDS,
    REFERENCE_NAME,
    assert_runtime_idle,
    align_hardware,
    assert_hardware_parity,
    guest_interface_name,
    remote_command,
    usable_guest_ipv4,
)


@pytest.mark.no_database
def test_shared_template_factory_requires_all_six_workers_idle():
    state = {
        "active_operations": 0,
        "queue_depth": 0,
        "uncertain_operations": 0,
        "workers": [
            {"status": "idle", "liveness": "healthy", "current_operation_id": None}
            for _ in range(6)
        ],
    }
    assert_runtime_idle(state)
    state["workers"][4]["status"] = "busy"
    with pytest.raises(RuntimeError, match="GOLDEN_RUNTIME_NOT_IDLE"):
        assert_runtime_idle(state)


def test_almalinux_factory_clones_current_vmids_to_temporary_candidates():
    assert COPIES == {
        9: {"source": 29010, "target": 29016},
        10: {"source": 29011, "target": 29017},
    }
    assert all(item["source"] != item["target"] for item in COPIES.values())
    assert {item["target"] for item in COPIES.values()}.isdisjoint(
        PROTECTED_BASE_VMIDS
    )
    assert {
        "policycoreutils-python-utils",
        "iproute",
        "cloud-init",
        "openssh-server",
    }.issubset(BASE_PACKAGES)
    assert CLOUD_INIT_SERVICES == (
        "cloud-init-local.service",
        "cloud-init.service",
        "cloud-config.service",
        "cloud-final.service",
    )
    assert GUEST_BOOTSTRAP_CHECKS == (("cloud-init", "--version"), ("ip", "-V"))


@pytest.mark.no_database
def test_initial_dhcp_interface_matches_the_cloned_mac():
    assert guest_interface_name(
        [
            {"name": "lo", "hardware-address": "00:00:00:00:00:00"},
            {"name": "ens18", "hardware-address": "BC:24:11:11:22:33"},
        ],
        "bc:24:11:11:22:33",
    ) == "ens18"
    with pytest.raises(RuntimeError, match="GOLDEN_GUEST_NIC_NOT_FOUND"):
        guest_interface_name([], "bc:24:11:11:22:33")
    assert usable_guest_ipv4("192.168.3.51")
    assert not usable_guest_ipv4("169.254.1.2")
    assert not usable_guest_ipv4("127.0.0.1")


def golden_config(name, vmid, mac):
    return {
        **GOLDEN_HARDWARE,
        "name": name,
        "template": 1,
        "scsi0": f"DS_CYBERFUEL:base-{vmid}-disk-1,discard=on,iothread=1,size=20G",
        "scsi1": f"DS_CYBERFUEL:vm-{vmid}-cloudinit,media=cdrom,size=4M",
        "efidisk0": f"DS_CYBERFUEL:base-{vmid}-disk-0,efitype=4m,pre-enrolled-keys=0,size=1M",
        "net0": f"virtio={mac},bridge=vmbr0",
    }


def test_almalinux_hardware_signature_matches_ubuntu_reference():
    reference = golden_config(REFERENCE_NAME, 29000, "BC:24:11:F0:88:41")
    candidate = golden_config("iacp-almalinux-9-golden", 29010, "BC:24:11:11:22:33")
    assert_hardware_parity(reference, candidate)


def test_almalinux_hardware_signature_rejects_controller_or_nic_drift():
    reference = golden_config(REFERENCE_NAME, 29000, "BC:24:11:F0:88:41")
    candidate = golden_config("iacp-almalinux-9-golden", 29010, "BC:24:11:11:22:33")
    candidate["scsihw"] = "virtio-scsi-pci"
    try:
        assert_hardware_parity(reference, candidate)
    except RuntimeError as error:
        assert "scsihw" in str(error)
    else:
        raise AssertionError("se aceptó un controlador SCSI distinto")


def test_almalinux_hardware_signature_rejects_ide_and_e1000():
    reference = golden_config(REFERENCE_NAME, 29000, "BC:24:11:F0:88:41")
    candidate = golden_config("iacp-almalinux-9-golden", 29010, "BC:24:11:11:22:33")
    candidate["ide2"] = "DS_CYBERFUEL:vm-29010-cloudinit,media=cdrom"
    candidate["net0"] = "e1000=BC:24:11:11:22:33,bridge=vmbr0"
    try:
        assert_hardware_parity(reference, candidate)
    except RuntimeError as error:
        assert "ide_devices" in str(error) and "network_model" in str(error)
    else:
        raise AssertionError("se aceptó IDE/e1000 en la Golden Image AlmaLinux")


def test_remote_command_round_trips_arguments_with_shell_metacharacters():
    arguments = [
        "printf",
        "%s\n",
        "espacio y 'comillas' \"$HOME\"; literal",
        "`id` $(touch /tmp/no-ejecutar)",
        "línea\nnueva",
    ]
    assert shlex.split(remote_command(arguments)) == arguments


def test_disk_resize_uses_proxmox_put_and_matches_ubuntu_hardware():
    vmid = 29010
    name = "iacp-almalinux-9-golden"

    class Client:
        def __init__(self):
            self.calls = []
            self.config = {
                "name": name,
                "scsihw": "virtio-scsi-pci",
                "cpu": "x86-64-v4",
                "cores": 2,
                "memory": "2048",
                "agent": "enabled=1",
                "boot": "order=scsi0",
                "serial0": "socket",
                "ipconfig0": "ip=dhcp",
                "scsi0": f"DS_CYBERFUEL:vm-{vmid}-disk-0,size=10G",
                "ide2": f"DS_CYBERFUEL:vm-{vmid}-cloudinit,media=cdrom,size=4M",
                "net0": "e1000=BC:24:11:47:48:8E,bridge=vmbr0",
                "vga": "serial0",
            }

        def vm_config(self, node, requested_vmid):
            assert node == "mng01" and requested_vmid == vmid
            return dict(self.config)

        def get(self, path):
            if path.endswith("/status/current"):
                return {"status": "stopped"}
            if path.endswith("/status"):
                return {"status": "stopped", "exitstatus": "OK"}
            raise AssertionError(f"GET inesperado: {path}")

        def put(self, path, parameters):
            self.calls.append(("PUT", path, parameters))
            if path.endswith("/resize"):
                assert parameters == {"disk": "scsi0", "size": "+10G"}
                self.config["scsi0"] += ",size=20G"
                return "UPID:mng01:1234:5678:90AB:resize:29010:root@pam:"
            assert path.endswith("/config")
            for key, value in parameters.items():
                if key == "delete":
                    for deleted in value.split(","):
                        self.config.pop(deleted, None)
                elif key == "scsi1" and value.startswith("DS_CYBERFUEL:cloudinit"):
                    self.config[key] = f"DS_CYBERFUEL:vm-{vmid}-cloudinit,media=cdrom"
                elif key == "scsi0":
                    self.config[key] = value + ",size=20G"
                elif key == "scsi1":
                    self.config[key] = value + ",size=4M"
                elif key == "efidisk0":
                    self.config[key] = (
                        f"DS_CYBERFUEL:vm-{vmid}-disk-1,{value.split(',', 1)[1]},size=1M"
                    )
                else:
                    self.config[key] = value
            return None

        def post(self, *_args, **_kwargs):
            raise AssertionError("resize no debe usar POST")

    reference = golden_config(REFERENCE_NAME, 29000, "BC:24:11:F0:88:41")
    client = Client()
    result = align_hardware(client, vmid, name, reference, version=9)

    assert [method for method, _path, _params in client.calls] == [
        "PUT",
        "PUT",
        "PUT",
        "PUT",
    ]
    assert client.calls[0][1].endswith("/resize")
    assert client.calls[1][2] == {"delete": "ide2"}
    assert client.calls[2][2] == {"scsi1": "DS_CYBERFUEL:cloudinit,media=cdrom"}
    assert result["scsihw"] == "virtio-scsi-single"
    assert result["net0"].startswith("virtio=")
    assert_hardware_parity(reference, result)


@pytest.mark.no_database
def test_initial_dhcp_waits_for_nic_during_first_boot(monkeypatch):
    """Clon AlmaLinux: el agente responde antes de que exista eth0 (relabel)."""
    from types import SimpleNamespace

    import scripts.prepare_golden_copies as factory_module

    mac = "bc:24:11:42:14:30"
    observations = iter([
        [{"name": "lo", "hardware-address": "00:00:00:00:00:00"}],
        [{"name": "lo", "hardware-address": "00:00:00:00:00:00"}],
        [{"name": "eth0", "hardware-address": mac.upper(),
          "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "192.168.3.127"}]}],
    ])
    current = {"value": None}

    def interfaces(_node, _vmid):
        try:
            current["value"] = next(observations)
        except StopIteration:
            pass
        return current["value"]

    monkeypatch.setattr(factory_module.time, "sleep", lambda _seconds: None)
    factory = factory_module.Factory.__new__(factory_module.Factory)
    factory.client = SimpleNamespace(guest_agent_interfaces=interfaces)
    factory.qga = lambda *args, **kwargs: pytest.fail("no debe forzar nmcli si ya hay IPv4")
    assert factory.ensure_initial_dhcp(29016, mac) == "192.168.3.127"


@pytest.mark.no_database
def test_initial_dhcp_still_fails_closed_without_nic(monkeypatch):
    from types import SimpleNamespace

    import scripts.prepare_golden_copies as factory_module

    clock = iter(range(0, 1000, 50))
    monkeypatch.setattr(factory_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(factory_module.time, "sleep", lambda _seconds: None)
    factory = factory_module.Factory.__new__(factory_module.Factory)
    factory.client = SimpleNamespace(guest_agent_interfaces=lambda *_: [])
    with pytest.raises(RuntimeError, match="GOLDEN_GUEST_NIC_NOT_FOUND"):
        factory.ensure_initial_dhcp(29016, "bc:24:11:42:14:30")
