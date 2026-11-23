import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from worker import preboot

pytestmark = pytest.mark.no_database


KEY = "ssh-ed25519 AAAAexample equipo"


@pytest.fixture
def contract():
    instance = {
        "vm_name": "preboot-nextcloud",
        "vm_id": 40123,
        "node_name": "mng01",
        "started": False,
        "cpu_cores": 2,
        "cpu_hotplugged": 2,
        "cpu_sockets": 1,
        "cpu_type": "x86-64-v4",
        "cpu_flags": ["+aes"],
        "numa": True,
        "memory_mb": 4096,
        "bios": "ovmf",
        "machine": "q35",
        "scsi_hardware": "virtio-scsi-single",
        "hotplug": "disk,network,usb,memory,cpu",
        "boot_order": ["scsi0"],
        "guest_agent_enabled": True,
        "tags": ["almalinux-9", "iacp"],
        "boot_disk": {
            "interface": "scsi0",
            "datastore_id": "DS_CYBERFUEL",
            "size_gb": 100,
            "aio": "io_uring",
            "cache": "none",
            "discard": "on",
            "backup": True,
            "replicate": True,
            "iothread": True,
            "ssd": False,
        },
        "cloudinit_datastore_id": "DS_CYBERFUEL",
        "cloudinit_username": "root",
        "ssh_public_keys": [KEY],
        "network_bridge": "vmbr3",
        "network_model": "virtio",
        "network_mode": "static",
        "vlan_id": None,
        "ipv4_cidr": "190.0.231.7/24",
        "ipv4_gateway": "190.0.231.1",
        "dns_servers": ["1.1.1.1", "8.8.8.8"],
    }
    golden = {
        "image_id": "almalinux-9",
        "ostype": "l26",
        "balloon_mb": 0,
        "on_boot": False,
        "serial_port": "socket",
        "efi_disk": {
            "datastore_id": "DS_CYBERFUEL",
            "efitype": "4m",
            "pre-enrolled-keys": "0",
            "size": "1M",
        },
        "cloudinit_interface": "scsi1",
        "cloudinit_datastore_id": "DS_CYBERFUEL",
    }
    expected = preboot.expectation(
        instance,
        golden,
        password_present=True,
        node="mng01",
        vmid=40123,
    )
    config = {
        "name": "preboot-nextcloud",
        "template": 0,
        "tags": "almalinux-9;iacp;mng01;app-nextcloud",
        "cores": 2,
        "sockets": 1,
        "cpu": "cputype=x86-64-v4,flags=+aes",
        "numa": 1,
        "memory": "4096",
        "bios": "ovmf",
        "machine": "q35",
        "scsihw": "virtio-scsi-single",
        "hotplug": "disk,network,usb,memory,cpu",
        "boot": "order=scsi0",
        "agent": "enabled=1,fstrim_cloned_disks=0,type=virtio",
        "ostype": "l26",
        "balloon": 0,
        "onboot": 0,
        "serial0": "socket",
        "efidisk0": "DS_CYBERFUEL:vm-40123-disk-0,efitype=4m,pre-enrolled-keys=0,size=1M",
        "scsi0": "DS_CYBERFUEL:vm-40123-disk-1,aio=io_uring,backup=1,cache=none,discard=on,iothread=1,replicate=1,size=100G,ssd=0",
        "scsi1": "DS_CYBERFUEL:vm-40123-cloudinit,media=cdrom",
        "net0": "virtio=BC:24:11:11:22:33,bridge=vmbr3,firewall=0",
        "ipconfig0": "gw=190.0.231.1,ip=190.0.231.7/24",
        "nameserver": "1.1.1.1 8.8.8.8",
        "ciuser": "root",
        "ciupgrade": 0,
        "cipassword": "opaque-secret-value",
        "sshkeys": "ssh-ed25519%20AAAAexample%20equipo",
    }
    return expected, config


def test_preboot_gate_accepts_complete_matching_stopped_vm(contract):
    expected, config = contract
    assert preboot.mismatches(
        config,
        expected,
        vmid=40123,
        node="mng01",
        name="preboot-nextcloud",
        power_state="stopped",
    ) == []


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("name", lambda c: c.update(name="other-vm")),
        ("cpu_cores", lambda c: c.update(cores=4)),
        ("memory_mb", lambda c: c.update(memory="8192")),
        ("scsi_hardware", lambda c: c.update(scsihw="lsi")),
        ("boot_disk_size", lambda c: c.update(scsi0=c["scsi0"].replace("100G", "50G"))),
        ("network_bridge", lambda c: c.update(net0=c["net0"].replace("vmbr3", "vmbr0"))),
        ("network_ip", lambda c: c.update(ipconfig0="ip=190.0.231.8/24,gw=190.0.231.1")),
        ("nameserver", lambda c: c.update(nameserver="1.1.1.1")),
        ("cloudinit_interface", lambda c: c.pop("scsi1")),
        ("cloudinit_password", lambda c: c.pop("cipassword")),
        ("cloudinit_ssh_keys", lambda c: c.update(sshkeys="ssh-ed25519%20different")),
        ("cloudinit_upgrade_disabled", lambda c: c.pop("ciupgrade")),
        ("cloudinit_upgrade_disabled", lambda c: c.update(ciupgrade=1)),
    ],
)
def test_preboot_gate_rejects_any_request_or_image_mismatch(contract, field, mutate):
    expected, config = contract
    mutate(config)
    failures = preboot.mismatches(
        config,
        expected,
        vmid=40123,
        node="mng01",
        name="preboot-nextcloud",
        power_state="stopped",
    )
    assert field in failures
    assert "opaque-secret-value" not in ",".join(failures)
    assert KEY not in ",".join(failures)


def test_preboot_gate_rejects_vm_that_is_not_stopped(contract):
    expected, config = contract
    assert "power_state" in preboot.mismatches(
        config,
        expected,
        vmid=40123,
        node="mng01",
        name="preboot-nextcloud",
        power_state="running",
    )


def test_preboot_gate_checks_dhcp_mode_without_inventing_static_fields(contract):
    expected, config = contract
    instance = expected["instance"]
    instance.update(
        network_mode="dhcp",
        ipv4_cidr=None,
        ipv4_gateway=None,
        dns_servers=[],
    )
    config["ipconfig0"] = "ip=dhcp"
    config.pop("nameserver")
    expected["password_present"] = False
    instance["ssh_public_keys"] = []
    config.pop("cipassword")
    config.pop("sshkeys")
    assert preboot.mismatches(
        config,
        expected,
        vmid=40123,
        node="mng01",
        name="preboot-nextcloud",
        power_state="stopped",
    ) == []


def test_plan_gate_requires_stopped_tfvars_and_stopped_vm_change(tmp_path, monkeypatch, contract):
    expected, _ = contract
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "generated.auto.tfvars.json").write_text(
        json.dumps({"instance": {
            "started": False, "vm_id": 40123, "vm_name": "preboot-nextcloud", "node_name": "mng01",
        }})
    )
    preboot.save_expectation(str(workspace), expected)
    plan = {
        "resource_changes": [
            {
                "address": "module.kvm.proxmox_virtual_environment_vm.this",
                "mode": "managed",
                "type": "proxmox_virtual_environment_vm",
                "name": "this",
                "change": {"actions": ["create"], "after": {
                    "started": False, "on_boot": False, "vm_id": 40123,
                    "name": "preboot-nextcloud", "node_name": "mng01",
                }},
            }
        ]
    }
    def inspect(*_args, **kwargs):
        assert "PROXMOX_VE_API_TOKEN" not in kwargs["env"]
        assert "TF_VAR_root_password" not in kwargs["env"]
        assert "TF_CLI_ARGS_show" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(plan))

    monkeypatch.setenv("PROXMOX_VE_API_TOKEN", "valor-sintetico")
    monkeypatch.setenv("TF_VAR_root_password", "valor-sintetico")
    monkeypatch.setenv("TF_CLI_ARGS_show", "-no-color")
    monkeypatch.setattr(preboot.subprocess, "run", inspect)
    target = (40123, "preboot-nextcloud", "mng01")
    plan_path = str(workspace / "plan.tfplan")
    preboot.assert_stopped_plan(str(workspace), plan_path, target)

    change = plan["resource_changes"][0]["change"]
    for field, value in {
        "vm_id": 40124, "name": "otra-instancia", "node_name": "mng02",
    }.items():
        with monkeypatch.context() as patch:
            patch.setitem(change["after"], field, value)
            with pytest.raises(RuntimeError, match="TERRAFORM_PLAN_IDENTITY_MISMATCH"):
                preboot.assert_stopped_plan(str(workspace), plan_path, target)

    for actions in (["delete", "create"], ["update"], ["delete"]):
        with monkeypatch.context() as patch:
            patch.setitem(change, "actions", actions)
            with pytest.raises(RuntimeError, match="TERRAFORM_UNEXPECTED_RESOURCE_CHANGE"):
                preboot.assert_stopped_plan(str(workspace), plan_path, target)

    with monkeypatch.context() as patch:
        patch.setitem(plan, "resource_changes", plan["resource_changes"] * 2)
        with pytest.raises(RuntimeError, match="TERRAFORM_UNEXPECTED_RESOURCE_CHANGE"):
            preboot.assert_stopped_plan(str(workspace), plan_path, target)

    change["after"]["started"] = True
    with pytest.raises(RuntimeError, match="TERRAFORM_START_BEFORE_VALIDATION_BLOCKED"):
        preboot.assert_stopped_plan(str(workspace), plan_path, target)


def test_plan_gate_rejects_old_plan_without_preboot_expectation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "generated.auto.tfvars.json").write_text(
        json.dumps({"instance": {"started": False}})
    )
    with pytest.raises(RuntimeError, match="PREBOOT_EXPECTATION_REQUIRED"):
        preboot.assert_stopped_plan(str(workspace), str(workspace / "plan.tfplan"))


def test_expectation_file_is_private_and_contains_no_password(contract, tmp_path):
    expected, _ = contract
    path = Path(preboot.save_expectation(str(tmp_path), expected))
    persisted = path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "opaque-secret-value" not in persisted
    assert expected["password_present"] is True


def test_preboot_no_bloquea_vms_previas_sin_marca_de_upgrade(contract):
    """Una expectativa anterior al cambio no exige ciupgrade=0 en un power start."""
    expected, config = contract
    legacy = {key: value for key, value in expected.items() if key != "cloudinit_upgrade_disabled"}
    config = dict(config)
    config.pop("ciupgrade")
    kwargs = {"vmid": 40123, "node": "mng01", "name": "preboot-nextcloud", "power_state": "stopped"}
    assert preboot.mismatches(config, legacy, **kwargs) == []
    assert preboot.mismatches(config, expected, **kwargs) == ["cloudinit_upgrade_disabled"]
