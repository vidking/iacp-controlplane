"""Validación fail-closed de la configuración de una KVM antes del primer inicio."""

from __future__ import annotations

import json
import os
import re
import tempfile
import subprocess
from pathlib import Path
from urllib.parse import unquote


EXPECTATION_FILENAME = "preboot.expectation.json"


def save_expectation(workspace: str, value: dict) -> str:
    """Persist the non-secret request/image contract beside its Terraform plan."""

    directory = Path(workspace)
    fd, temporary = tempfile.mkstemp(prefix=".preboot-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
        path = directory / EXPECTATION_FILENAME
        os.replace(temporary, path)
        return str(path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_expectation(workspace: str) -> dict:
    try:
        value = json.loads(
            (Path(workspace) / EXPECTATION_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        raise RuntimeError("PREBOOT_EXPECTATION_REQUIRED") from None
    if not isinstance(value, dict) or not isinstance(value.get("instance"), dict):
        raise RuntimeError("PREBOOT_EXPECTATION_REQUIRED")
    return value


def expectation(
    instance: dict,
    golden: dict,
    *,
    password_present: bool | None,
    node: str,
    vmid: int,
) -> dict:
    """Unir el contrato del portal con la huella no secreta de la Golden Image."""

    return {
        "instance": dict(instance),
        "image_id": golden.get("image_id"),
        "node": node,
        "vmid": int(vmid),
        "password_present": password_present,
        "image": {
            "ostype": golden.get("ostype"),
            "balloon_mb": golden.get("balloon_mb", 0),
            "on_boot": golden.get("on_boot", False),
            "serial_port": golden.get("serial_port"),
            "efi_disk": dict(golden.get("efi_disk") or {}),
            "cloudinit_interface": golden.get("cloudinit_interface", "scsi1"),
            "cloudinit_datastore_id": golden.get("cloudinit_datastore_id"),
        },
    }


def _options(raw: str | None) -> tuple[str, dict[str, str]]:
    if not raw:
        return "", {}
    parts = str(raw).split(",")
    first = parts[0]
    values = {
        key: value
        for key, value in (item.split("=", 1) for item in parts if "=" in item)
    }
    return first, values


def _int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _boolean(value: object) -> bool | None:
    if value in {1, "1", True, "true", "yes", "on"}:
        return True
    if value in {0, "0", False, "false", "no", "off", None, ""}:
        return False
    return None


def _cpu(raw: str | None) -> tuple[str, set[str]]:
    first, options = _options(raw)
    cpu_type = options.get("cputype") or (
        first.split("=", 1)[1] if first.startswith("cputype=") else first
    )
    flags = set(filter(None, re.split(r"[;,]", options.get("flags", ""))))
    return cpu_type, flags


def _disk(raw: str | None) -> tuple[str | None, dict[str, str]]:
    first, options = _options(raw)
    datastore_id = first.split(":", 1)[0] if ":" in first else None
    return datastore_id, options


def _boot_order(raw: str | None) -> list[str] | None:
    _, options = _options(raw)
    order = options.get("order")
    return [item for item in (order or "").split(";") if item] or None


def _ipconfig(raw: str | None) -> dict[str, str]:
    _, options = _options(raw)
    return options


def _normalized_keys(raw: object) -> set[str]:
    if not raw:
        return set()
    decoded = unquote(str(raw)).replace("\r", "")
    return {" ".join(line.split()) for line in decoded.splitlines() if line.strip()}


def mismatches(
    config: dict,
    expected: dict,
    *,
    vmid: int,
    node: str,
    name: str,
    power_state: str,
    require_stopped: bool = True,
) -> list[str]:
    """List field names that differ; never include credential values."""

    instance = expected.get("instance")
    image = expected.get("image")
    if not isinstance(instance, dict) or not isinstance(image, dict):
        return ["expectation"]

    failures: list[str] = []

    def check(field: str, condition: bool) -> None:
        if not condition:
            failures.append(field)

    check("name", config.get("name") == name == instance.get("vm_name"))
    check("vmid", _int(expected.get("vmid")) == int(vmid) == _int(instance.get("vm_id")))
    check("node", expected.get("node") == node == instance.get("node_name"))
    check("template", config.get("template") not in {1, "1", True})
    check("power_state", power_state == "stopped" if require_stopped else power_state in {"stopped", "running"})
    check("terraform_started", instance.get("started") is False)
    check("cpu_sockets", _int(config.get("sockets", 1)) == _int(instance.get("cpu_sockets", 1)))
    expected_cores = _int(instance.get("cpu_cores"))
    expected_sockets = max(1, _int(instance.get("cpu_sockets", 1)) or 1)
    check("cpu_cores", expected_cores is not None and _int(config.get("cores")) == expected_cores // expected_sockets)
    actual_cpu_type, actual_flags = _cpu(config.get("cpu"))
    check("cpu_type", actual_cpu_type == instance.get("cpu_type"))
    check("cpu_flags", actual_flags == set(instance.get("cpu_flags") or []))
    configured_vcpus = _int(config.get("vcpus"))
    expected_vcpus = _int(instance.get("cpu_hotplugged"))
    if expected_vcpus is not None and configured_vcpus is not None:
        check("cpu_hotplugged", configured_vcpus == expected_vcpus)
    check("numa", _boolean(config.get("numa", 0)) == bool(instance.get("numa", False)))
    check("memory_mb", _int(config.get("memory")) == _int(instance.get("memory_mb")))
    check("bios", config.get("bios") == instance.get("bios"))
    check("machine", config.get("machine") == instance.get("machine"))
    check("scsi_hardware", config.get("scsihw") == instance.get("scsi_hardware"))
    check("hotplug", set(str(config.get("hotplug", "")).split(",")) == set(str(instance.get("hotplug", "")).split(",")))
    check("boot_order", _boot_order(config.get("boot")) == instance.get("boot_order"))
    check("agent", str(config.get("agent", "")).startswith("enabled=1") == bool(instance.get("guest_agent_enabled")))
    check("ostype", config.get("ostype") == image.get("ostype"))
    check("balloon", _int(config.get("balloon", 0)) == _int(image.get("balloon_mb", 0)))
    check("on_boot", _boolean(config.get("onboot", 0)) == bool(image.get("on_boot", False)))
    if image.get("serial_port") is not None:
        check("serial_port", config.get("serial0") == image.get("serial_port"))

    expected_tags = set(instance.get("tags") or [])
    observed_tags = {tag for tag in str(config.get("tags", "")).split(";") if tag}
    check("tags", expected_tags <= observed_tags)

    disk = instance.get("boot_disk")
    if not isinstance(disk, dict):
        failures.append("boot_disk_expectation")
    else:
        interface = str(disk.get("interface") or "")
        storage, actual_disk = _disk(config.get(interface))
        check("boot_disk_interface", interface == "scsi0")
        check("boot_disk_storage", storage == disk.get("datastore_id"))
        size = re.fullmatch(r"(\d+)G", actual_disk.get("size", ""))
        check("boot_disk_size", bool(size) and int(size.group(1)) == _int(disk.get("size_gb")))
        disk_options = {
            "file_format": ("format", None),
            "aio": ("aio", None),
            "cache": ("cache", None),
            "discard": ("discard", None),
            "backup": ("backup", None),
            "replicate": ("replicate", None),
            "iothread": ("iothread", None),
            "ssd": ("ssd", None),
        }
        for expected_name, (actual_name, _) in disk_options.items():
            wanted = disk.get(expected_name)
            if wanted is None:
                continue
            if isinstance(wanted, bool):
                observed = _boolean(actual_disk.get(actual_name))
            else:
                observed = actual_disk.get(actual_name)
            check(f"boot_disk_{expected_name}", observed == wanted)

    # El estándar vigente usa SCSI0 para el disco y SCSI1 para cloud-init.
    cloud_interface = image.get("cloudinit_interface")
    check("cloudinit_interface", bool(config.get(str(cloud_interface or ""))))
    cloud_storage, cloud_options = _disk(config.get(str(cloud_interface or "")))
    check("cloudinit_interface_standard", cloud_interface == "scsi1")
    check("cloudinit_media", cloud_options.get("media") == "cdrom")
    check("cloudinit_storage", cloud_storage == instance.get("cloudinit_datastore_id"))
    check("cloudinit_no_ide", not config.get("ide2"))

    efi = image.get("efi_disk") or {}
    efi_storage, actual_efi = _disk(config.get("efidisk0"))
    check("efi_disk_storage", efi_storage == efi.get("datastore_id"))
    for field in ("efitype", "pre-enrolled-keys", "size"):
        check(f"efi_disk_{field}", actual_efi.get(field) == efi.get(field))

    nic_devices = sorted(key for key in config if re.fullmatch(r"net\d+", key))
    check("network_devices", nic_devices == ["net0"])
    nic, nic_options = _options(config.get("net0"))
    model, address = nic.split("=", 1) if "=" in nic else ("", "")
    check("network_model", model == instance.get("network_model"))
    check("network_mac", bool(re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", address)))
    check("network_bridge", nic_options.get("bridge") == instance.get("network_bridge"))
    vlan_id = instance.get("vlan_id")
    check("network_vlan", nic_options.get("tag") == (str(vlan_id) if vlan_id is not None else None))

    ipconfig = _ipconfig(config.get("ipconfig0"))
    if instance.get("network_mode", "dhcp") == "dhcp":
        check("network_ip_mode", ipconfig.get("ip") == "dhcp" and "gw" not in ipconfig)
    else:
        check("network_ip", ipconfig.get("ip") == instance.get("ipv4_cidr"))
        check("network_gateway", ipconfig.get("gw") == instance.get("ipv4_gateway"))

    dns_servers = instance.get("dns_servers") or []
    if dns_servers:
        check("nameserver", str(config.get("nameserver") or "").split() == list(dns_servers))
    check("cloudinit_user", config.get("ciuser") == instance.get("cloudinit_username"))
    password_present = expected.get("password_present")
    if password_present is not None:
        check("cloudinit_password", bool(config.get("cipassword")) == bool(password_present))
    check(
        "cloudinit_ssh_keys",
        _normalized_keys(config.get("sshkeys"))
        == {" ".join(key.split()) for key in instance.get("ssh_public_keys", [])},
    )

    return failures


def assert_stopped_plan(workspace: str, plan_path: str) -> None:
    """No permitir aplicar ningún plan Terraform que pretenda encender la VM."""

    variables_path = Path(workspace) / "generated.auto.tfvars.json"
    try:
        values = json.loads(variables_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("TERRAFORM_START_BEFORE_VALIDATION_BLOCKED") from None
    if not isinstance(values, dict) or values.get("instance", {}).get("started") is not False:
        raise RuntimeError("TERRAFORM_START_BEFORE_VALIDATION_BLOCKED")
    expected = load_expectation(workspace)
    expected_instance = expected.get("instance") or {}
    if (
        expected_instance.get("started") is not False
        or expected_instance.get("vm_id") != values.get("instance", {}).get("vm_id")
        or expected_instance.get("vm_name") != values.get("instance", {}).get("vm_name")
    ):
        raise RuntimeError("TERRAFORM_START_BEFORE_VALIDATION_BLOCKED")

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("TF_VAR_")
    }
    try:
        shown = subprocess.run(
            ["terraform", "show", "-json", os.path.abspath(plan_path)],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("TERRAFORM_PLAN_INSPECTION_FAILED") from None
    if shown.returncode != 0:
        raise RuntimeError("TERRAFORM_PLAN_INSPECTION_FAILED")
    try:
        plan = json.loads(shown.stdout)
    except (TypeError, ValueError):
        raise RuntimeError("TERRAFORM_PLAN_INSPECTION_FAILED") from None

    resources = [
        resource
        for resource in plan.get("resource_changes", [])
        if isinstance(resource, dict)
        and resource.get("type") == "proxmox_virtual_environment_vm"
        and resource.get("name") == "this"
        and set((resource.get("change") or {}).get("actions") or []) & {"create", "update"}
    ]
    if not resources:
        raise RuntimeError("TERRAFORM_VM_CHANGE_NOT_FOUND")
    if any(
        not isinstance((resource.get("change") or {}).get("after"), dict)
        or (resource.get("change") or {}).get("after", {}).get("started") is not False
        for resource in resources
    ):
        raise RuntimeError("TERRAFORM_START_BEFORE_VALIDATION_BLOCKED")
