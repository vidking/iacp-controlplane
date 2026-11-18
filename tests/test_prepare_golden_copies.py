import shlex

from scripts.prepare_golden_copies import (
    BASE_PACKAGES,
    COPIES,
    GOLDEN_HARDWARE,
    PROTECTED_BASE_VMIDS,
    REFERENCE_NAME,
    align_hardware,
    assert_hardware_parity,
    remote_command,
)


def test_almalinux_network_bootstrap_uses_new_protected_copy_targets():
    assert COPIES == {
        9: {"source": 29016, "target": 29010},
        10: {"source": 29017, "target": 29011},
    }
    assert all(item["source"] != item["target"] for item in COPIES.values())
    assert {item["target"] for item in COPIES.values()}.isdisjoint(
        PROTECTED_BASE_VMIDS
    )
    assert {"policycoreutils-python-utils", "iproute"}.issubset(BASE_PACKAGES)


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
    result = align_hardware(client, vmid, name, reference)

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
