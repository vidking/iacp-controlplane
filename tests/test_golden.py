import pytest

from worker import golden


@pytest.fixture
def golden_source():
    raw = {
        "name": "iacp-almalinux-9-golden",
        "template": 1,
        "tags": "almalinux-9;iacp;golden-image",
        "cpu": "x86-64-v4,flags=+aes",
        "cores": 10,
        "sockets": 1,
        "vcpus": 2,
        "numa": 1,
        "memory": 4096,
        "bios": "ovmf",
        "machine": "q35",
        "scsihw": "virtio-scsi-single",
        "boot": "order=scsi0",
        "agent": "enabled=1,fstrim_cloned_disks=0,type=virtio",
        "onboot": 0,
        "serial0": "socket",
        "hotplug": "disk,network,usb,memory,cpu",
        "ipconfig0": "ip=dhcp",
        "scsi0": "DS_CYBERFUEL:base-29010-disk-1,discard=on,iothread=1,size=20G",
        "scsi1": "DS_CYBERFUEL:vm-29010-cloudinit,media=cdrom,size=4M",
        "efidisk0": "DS_CYBERFUEL:base-29010-disk-0,efitype=4m,pre-enrolled-keys=0,size=1M",
        "net0": "virtio=BC:24:11:11:22:33,bridge=vmbr0",
    }
    entry = {
        "source_node": "mng01",
        "template_vm_id": 29010,
        "tag": "almalinux-9",
        "storage": "DS_CYBERFUEL",
        "network_model": "virtio",
    }
    return raw, entry


def observe_with(golden_source, monkeypatch):
    raw, entry = golden_source
    reference = dict(raw)
    reference.update(
        name="iacp-ubuntu-2404-golden",
        tags="ubuntu-2404;iacp;golden-image",
        scsi0="DS_CYBERFUEL:base-29000-disk-1,discard=on,iothread=1,size=20G",
        scsi1="DS_CYBERFUEL:vm-29000-cloudinit,media=cdrom,size=4M",
        efidisk0="DS_CYBERFUEL:base-29000-disk-0,efitype=4m,pre-enrolled-keys=0,size=1M",
        net0="virtio=BC:24:11:00:00:01,bridge=vmbr0",
    )
    image_catalog = {
        "almalinux-9": entry,
        "ubuntu-2404": {
            "source_node": "mng01",
            "template_vm_id": 29000,
            "tag": "ubuntu-2404",
            "storage": "DS_CYBERFUEL",
            "network_model": "virtio",
        },
    }
    monkeypatch.setattr(golden, "catalog", lambda: image_catalog)

    class Client:
        def vm_config(self, node, vmid):
            assert node == "mng01"
            if vmid == 29010:
                return raw
            if vmid == 29000:
                return reference
            pytest.fail(f"VMID de plantilla inesperado: {vmid}")

    return golden.observe("almalinux-9", Client())


@pytest.mark.no_database
def test_observe_accepts_template_matching_the_certified_ubuntu_hardware(golden_source, monkeypatch):
    observed = observe_with(golden_source, monkeypatch)
    assert observed["image_id"] == "almalinux-9"
    assert observed["network_model"] == "virtio"
    assert observed["scsi_hardware"] == "virtio-scsi-single"


@pytest.mark.no_database
def test_observe_normalizes_equivalent_proxmox_defaults(golden_source, monkeypatch):
    raw, _entry = golden_source
    raw["onboot"] = None
    raw["scsi1"] = raw["scsi1"].replace(",size=4M", "")

    observed = observe_with(golden_source, monkeypatch)

    assert observed["image_id"] == "almalinux-9"


@pytest.mark.no_database
@pytest.mark.parametrize(
    ("field", "value", "expected_mismatch"),
    [
        ("scsihw", "virtio-scsi-pci", "scsihw"),
        ("net0", "e1000=BC:24:11:11:22:33,bridge=vmbr0", "network"),
        ("ide2", "DS_CYBERFUEL:vm-29010-cloudinit,media=cdrom", "ide_devices"),
        ("boot", "order=scsi1", "boot"),
        ("onboot", 1, "onboot"),
    ],
)
def test_observe_rejects_template_hardware_drift(golden_source, monkeypatch, field, value, expected_mismatch):
    raw, _entry = golden_source
    raw[field] = value
    with pytest.raises(ValueError, match="GOLDEN_HARDWARE_NOT_CERTIFIED") as error:
        observe_with(golden_source, monkeypatch)
    assert expected_mismatch in str(error.value)


@pytest.mark.no_database
def test_observe_rejects_hardware_difference_from_ubuntu_reference(golden_source, monkeypatch):
    raw, _entry = golden_source
    raw["scsi0"] = raw["scsi0"].replace("discard=on", "cache=writeback,discard=on")
    with pytest.raises(ValueError, match="GOLDEN_HARDWARE_REFERENCE_MISMATCH"):
        observe_with(golden_source, monkeypatch)
