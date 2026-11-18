import os
import re

from api.catalog import cluster_nodes, load_catalog, prod_static_proxmox_network
from worker.proxmox import ProxmoxClient, ProxmoxError


DEFAULT_POOL_ID = "iacp-lab"


def catalog() -> dict[str, dict]:
    return {item["image_id"]: item for item in load_catalog()["images"]}


def configured_pool_id() -> str:
    pool_id = os.getenv("IACP_PROXMOX_POOL_ID", DEFAULT_POOL_ID).strip()
    if not pool_id or not re.fullmatch(r"[A-Za-z0-9_-]+", pool_id):
        raise ValueError("PROXMOX_POOL_REQUIRED")
    return pool_id


def validate_pool(client: ProxmoxClient) -> str:
    pool_id = configured_pool_id()
    if not any(str(pool.get("poolid")) == pool_id for pool in client.pools() if isinstance(pool, dict)):
        raise ProxmoxError("PROXMOX_POOL_NOT_FOUND", 404)
    return pool_id


def _disk(raw: str, interface: str) -> dict:
    parts = raw.split(",")
    datastore, volume = parts[0].split(":", 1)
    values = {key: value for key, value in (part.split("=", 1) for part in parts[1:] if "=" in part)}
    size = int(re.match(r"(\d+)G", values["size"]).group(1))
    return {
        "interface": interface,
        "datastore_id": datastore,
        "volume": volume,
        "size_gb": size,
        "minimum_size_gb": size,
        "file_format": values.get("format"),
        "aio": values.get("aio", "io_uring"),
        "cache": values.get("cache", "none"),
        "discard": values.get("discard", "ignore"),
        "backup": values.get("backup", "1") == "1",
        "replicate": values.get("replicate", "1") == "1",
        "iothread": values.get("iothread", "0") == "1",
        "ssd": values.get("ssd", "0") == "1",
    }


def _boot_order(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    match = re.search(r"(?:^|,)order=([^,]+)", raw)
    if not match:
        return None
    order = [item for item in match.group(1).split(";") if item]
    return order or None


def _device(raw: str | None) -> dict:
    if not raw:
        return {}
    first, *rest = raw.split(",")
    datastore_id = first.split(":", 1)[0] if ":" in first else None
    values = {
        key: value
        for key, value in (part.split("=", 1) for part in rest if "=" in part)
    }
    return {"datastore_id": datastore_id, **values}


def _certified_hardware_mismatches(raw: dict, disk: dict, cloudinit: dict, efi_disk: dict) -> list[str]:
    """Comparar la plantilla con el estándar aprobado de Ubuntu 24.04."""

    failures = []
    expected_values = {
        "bios": "ovmf",
        "machine": "q35",
        "scsihw": "virtio-scsi-single",
        "cores": "10",
        "sockets": "1",
        "vcpus": "2",
        "numa": "1",
        "memory": "4096",
        "boot": "order=scsi0",
        "serial0": "socket",
        "hotplug": "disk,network,usb,memory,cpu",
        "ipconfig0": "ip=dhcp",
    }
    failures.extend(
        field for field, wanted in expected_values.items()
        if str(raw.get(field, "")) != wanted
    )
    cpu_type, _, cpu_options = str(raw.get("cpu", "")).partition(",")
    if cpu_type != "x86-64-v4" or cpu_options != "flags=+aes":
        failures.append("cpu")
    if not str(raw.get("agent", "")).startswith("enabled=1"):
        failures.append("agent")
    if raw.get("onboot") not in {None, 0, "0", False}:
        failures.append("onboot")
    if disk.get("interface") != "scsi0" or disk.get("size_gb") != 20:
        failures.append("boot_disk")
    if disk.get("discard") != "on" or disk.get("iothread") is not True:
        failures.append("boot_disk_options")
    if cloudinit.get("media") != "cdrom" or cloudinit.get("datastore_id") != disk.get("datastore_id"):
        failures.append("cloudinit")
    if efi_disk != {
        "datastore_id": disk.get("datastore_id"),
        "efitype": "4m",
        "pre-enrolled-keys": "0",
        "size": "1M",
    }:
        failures.append("efi_disk")
    network = str(raw.get("net0", "")).split(",")
    model, separator, _mac = network[0].partition("=") if network else ("", "", "")
    network_options = {
        key: value
        for key, value in (part.split("=", 1) for part in network[1:] if "=" in part)
    }
    if model != "virtio" or not separator or network_options != {"bridge": "vmbr0"}:
        failures.append("network")
    if any(re.fullmatch(r"ide[0-3]", key) for key in raw):
        failures.append("ide_devices")
    if raw.get("vga") is not None:
        failures.append("vga")
    return failures


def _hardware_signature(raw: dict) -> dict:
    """Normalizar el hardware sin comparar MAC ni nombres de volúmenes clonados."""

    signature = {
        field: raw.get(field)
        for field in (
            "bios",
            "machine",
            "ostype",
            "scsihw",
            "cores",
            "sockets",
            "vcpus",
            "cpu",
            "numa",
            "memory",
            "balloon",
            "agent",
            "onboot",
            "boot",
            "hotplug",
            "ipconfig0",
            "vga",
        )
    }
    # Proxmox omite estos valores cuando usa el mismo default efectivo:
    # onboot=0 y tamaño 4M para la unidad Cloud-Init.
    onboot = raw.get("onboot")
    if onboot in {None, 0, "0", False}:
        signature["onboot"] = False
    elif onboot in {1, "1", True}:
        signature["onboot"] = True
    if signature.get("onboot") is None:
        signature["onboot"] = False
    devices = {}
    for key, value in raw.items():
        if re.fullmatch(r"(?:ide|sata|virtio|scsi|efidisk|tpmstate|rng)\d+", key):
            parsed = _device(value)
            options = {name: option for name, option in parsed.items() if name != "datastore_id"}
            if key == "scsi1" and options.get("media") == "cdrom":
                options.setdefault("size", "4M")
            devices[key] = {
                "datastore_id": parsed.get("datastore_id"),
                "options": options,
            }
        elif re.fullmatch(r"net\d+", key):
            model, separator, options = str(value or "").partition("=")
            _mac, comma, remainder = options.partition(",")
            devices[key] = {
                "model": model if separator else "",
                "options": {
                    name: option
                    for name, option in (
                        part.split("=", 1)
                        for part in (remainder.split(",") if comma else [])
                        if "=" in part
                    )
                },
            }
        elif re.fullmatch(r"serial\d+", key):
            devices[key] = value
    signature["devices"] = devices
    return signature


def observe(image_id: str, client: ProxmoxClient) -> dict:
    image_catalog = catalog()
    entry = image_catalog.get(image_id)
    if entry is None:
        raise ValueError("IMAGE_NOT_AVAILABLE")
    node = entry["source_node"]
    vmid = int(entry["template_vm_id"])
    raw = client.vm_config(node, vmid)
    if raw.get("template") != 1:
        raise ValueError("GOLDEN_NOT_TEMPLATE")
    tags = {tag for tag in str(raw.get("tags", "")).split(";") if tag}
    expected_tag = entry["tag"]
    if expected_tag not in tags:
        raise ValueError("GOLDEN_TAG_MISMATCH")
    cpu_parts = str(raw.get("cpu", "")).split(",")
    cpu_type = cpu_parts[0]
    cpu_options = {key: value for key, value in (part.split("=", 1) for part in cpu_parts[1:] if "=" in part)}
    flags = [value for value in cpu_options.get("flags", "").split(";") if value]
    disk_interface = next((name for name in ("scsi0", "sata0", "virtio0") if raw.get(name)), None)
    if disk_interface is None:
        raise ValueError("GOLDEN_BOOT_DISK_MISSING")
    disk = _disk(raw[disk_interface], disk_interface)
    if disk["datastore_id"] != entry["storage"]:
        raise ValueError("GOLDEN_STORAGE_MISMATCH")
    configured_model = str(raw.get("net0", "")).split("=", 1)[0].strip()
    network_model = entry.get("network_model") or configured_model or "virtio"
    cloudinit = _device(raw.get("scsi1"))
    if not cloudinit or cloudinit.get("media") != "cdrom":
        raise ValueError("GOLDEN_CLOUDINIT_LAYOUT_INVALID")
    efi_disk = _device(raw.get("efidisk0"))
    if not efi_disk:
        raise ValueError("GOLDEN_EFI_DISK_MISSING")
    hardware_failures = _certified_hardware_mismatches(raw, disk, cloudinit, efi_disk)
    if hardware_failures:
        raise ValueError("GOLDEN_HARDWARE_NOT_CERTIFIED:" + ",".join(hardware_failures))
    if image_id != "ubuntu-2404":
        reference = image_catalog.get("ubuntu-2404")
        if reference is None:
            raise ValueError("GOLDEN_REFERENCE_IMAGE_UNAVAILABLE")
        reference_raw = client.vm_config(reference["source_node"], int(reference["template_vm_id"]))
        if reference_raw.get("template") != 1:
            raise ValueError("GOLDEN_REFERENCE_IMAGE_NOT_TEMPLATE")
        reference_disk_interface = next(
            (name for name in ("scsi0", "sata0", "virtio0") if reference_raw.get(name)),
            None,
        )
        if reference_disk_interface is None:
            raise ValueError("GOLDEN_REFERENCE_BOOT_DISK_MISSING")
        reference_disk = _disk(reference_raw[reference_disk_interface], reference_disk_interface)
        reference_cloudinit = _device(reference_raw.get("scsi1"))
        reference_efi = _device(reference_raw.get("efidisk0"))
        reference_failures = _certified_hardware_mismatches(
            reference_raw, reference_disk, reference_cloudinit, reference_efi
        )
        if reference_failures:
            raise ValueError("GOLDEN_REFERENCE_NOT_CERTIFIED:" + ",".join(reference_failures))
        if _hardware_signature(raw) != _hardware_signature(reference_raw):
            raise ValueError("GOLDEN_HARDWARE_REFERENCE_MISMATCH")
    return {
        "image_id": image_id,
        "template_vm_id": vmid,
        "template_node": node,
        "vmtag": expected_tag,
        "cpu_type": cpu_type,
        "cpu_flags": flags,
        "numa": bool(int(raw.get("numa", 0))),
        "cpu_sockets": int(raw.get("sockets", 1)),
        "memory_mb": int(raw.get("memory", 0)),
        "bios": raw.get("bios"),
        "machine": raw.get("machine"),
        "scsi_hardware": raw.get("scsihw"),
        "hotplug": raw.get("hotplug"),
        "boot_order": _boot_order(raw.get("boot")),
        "agent_enabled": str(raw.get("agent", "")).startswith("enabled=1"),
        "ostype": raw.get("ostype"),
        "balloon_mb": int(raw.get("balloon", 0)),
        "on_boot": str(raw.get("onboot", "0")) == "1",
        "serial_port": raw.get("serial0"),
        "efi_disk": efi_disk,
        "cloudinit_interface": "scsi1",
        "cloudinit_datastore_id": cloudinit.get("datastore_id"),
        "network_bridge": str(raw.get("net0", "")).split("bridge=", 1)[-1].split(",", 1)[0],
        "network_model": network_model,
        "disk": disk,
        "tags": sorted(tags),
    }


def variables(
    spec: dict,
    vmid: int,
    golden: dict,
    stack: str = "mng01",
    *,
    target_node: str | None = None,
    allowed_nodes: list[str] | None = None,
    has_root_password: bool = False,
    pool_id: str | None = None,
) -> dict:
    image_id = spec["image"]
    username = catalog().get(image_id, {}).get("cloudinit_username", "root")
    target_node = target_node or "mng01"
    allowed_nodes = allowed_nodes or cluster_nodes()
    keys = spec.get("ssh_public_keys") or []
    if not keys:
        key_file = os.getenv("IACP_DEFAULT_SSH_PUBLIC_KEY_FILE", "")
        if key_file:
            with open(key_file, encoding="utf-8") as handle:
                keys = [handle.read().strip()]
    if not keys and os.getenv("IACP_DEFAULT_SSH_PUBLIC_KEY", "").strip():
        keys = [os.environ["IACP_DEFAULT_SSH_PUBLIC_KEY"].strip()]
    if not keys and not has_root_password:
        raise ValueError("SSH_PUBLIC_KEY_REQUIRED")
    pool_id = pool_id or configured_pool_id()
    disk = dict(golden["disk"])
    disk["size_gb"] = spec["disk_gb"]
    disk.pop("volume", None)
    return {
        "environment": stack,
        "allowed_nodes": allowed_nodes,
        "instance": {
            "vm_name": spec["name"],
            "vm_id": vmid,
            "node_name": target_node,
            "template_vm_id": golden["template_vm_id"],
            "template_node_name": golden["template_node"],
            "pool_id": pool_id,
            "clone_datastore_id": golden["disk"]["datastore_id"],
            "cpu_cores": spec["vcpus"],
            "cpu_type": golden["cpu_type"],
            "cpu_flags": golden["cpu_flags"],
            "cpu_sockets": golden["cpu_sockets"],
            "numa": golden["numa"],
            "cpu_hotplugged": spec["vcpus"],
            "memory_mb": spec["memory_mb"],
            "network_bridge": (
                prod_static_proxmox_network()["bridge"]
                if spec.get("network_profile") == "prod_static"
                else (golden["network_bridge"] or "vmbr0")
            ),
            "network_model": golden.get("network_model", "virtio"),
            "network_mode": spec.get("network_mode", "dhcp"),
            "vlan_id": spec.get("vlan_id"),
            "ipv4_cidr": spec.get("ipv4_cidr"),
            "ipv4_gateway": spec.get("ipv4_gateway"),
            "cloudinit_datastore_id": golden["disk"]["datastore_id"],
            "cloudinit_username": username,
            "dns_servers": spec.get("dns_servers", []),
            "ssh_public_keys": keys,
            "guest_agent_enabled": golden["agent_enabled"],
            # Every new KVM stays stopped until the worker compares the full
            # Proxmox configuration with this request and the certified image.
            # The requested state is applied only after that fail-closed gate.
            "started": False,
            "bios": golden.get("bios"),
            "machine": golden.get("machine"),
            "scsi_hardware": golden.get("scsi_hardware"),
            "hotplug": golden.get("hotplug"),
            "boot_order": golden.get("boot_order"),
            "tags": [golden["vmtag"], *spec.get("tags", [])],
            "boot_disk": disk,
        },
    }
