import os
import re

from api.catalog import load_catalog
from worker.proxmox import ProxmoxClient, ProxmoxError


def catalog() -> dict[str, dict]:
    return {item["image_id"]: item for item in load_catalog()["images"]}


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


def observe(image_id: str, client: ProxmoxClient) -> dict:
    entry = catalog().get(image_id)
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
        "network_bridge": str(raw.get("net0", "")).split("bridge=", 1)[-1].split(",", 1)[0],
        "disk": disk,
        "tags": sorted(tags),
    }


def variables(spec: dict, vmid: int, golden: dict, stack: str = "mng01") -> dict:
    image_id = spec["image"]
    image = catalog().get(image_id)
    if image is None:
        raise ValueError("IMAGE_NOT_AVAILABLE")
    username = image["cloudinit_username"]
    keys = spec.get("ssh_public_keys") or []
    if not keys:
        key_file = os.getenv("IACP_DEFAULT_SSH_PUBLIC_KEY_FILE", "")
        if key_file:
            with open(key_file, encoding="utf-8") as handle:
                keys = [handle.read().strip()]
    if not keys and os.getenv("IACP_DEFAULT_SSH_PUBLIC_KEY", "").strip():
        keys = [os.environ["IACP_DEFAULT_SSH_PUBLIC_KEY"].strip()]
    if not keys:
        raise ValueError("SSH_PUBLIC_KEY_REQUIRED")
    disk = dict(golden["disk"])
    disk["size_gb"] = spec["disk_gb"]
    disk.pop("volume", None)
    return {
        "environment": stack,
        "allowed_nodes": ["mng01"],
        "instance": {
            "vm_name": spec["name"],
            "vm_id": vmid,
            "node_name": "mng01",
            "template_vm_id": golden["template_vm_id"],
            "template_node_name": golden["template_node"],
            "pool_id": None,
            "clone_datastore_id": golden["disk"]["datastore_id"],
            "cpu_cores": spec["vcpus"],
            "cpu_type": golden["cpu_type"],
            "cpu_flags": golden["cpu_flags"],
            "cpu_sockets": golden["cpu_sockets"],
            "numa": golden["numa"],
            "cpu_hotplugged": spec["vcpus"],
            "memory_mb": spec["memory_mb"],
            "network_bridge": golden["network_bridge"] or "vmbr0",
            "network_mode": spec.get("network_mode", "dhcp"),
            "vlan_id": spec.get("vlan_id"),
            "ipv4_cidr": spec.get("ipv4_cidr"),
            "ipv4_gateway": spec.get("ipv4_gateway"),
            "cloudinit_datastore_id": golden["disk"]["datastore_id"],
            "cloudinit_username": username,
            "ssh_public_keys": keys,
            "guest_agent_enabled": golden["agent_enabled"],
            "started": spec.get("started", True),
            "bios": golden.get("bios"),
            "machine": golden.get("machine"),
            "scsi_hardware": golden.get("scsi_hardware"),
            "hotplug": golden.get("hotplug"),
            "boot_order": golden.get("boot_order"),
            "tags": [golden["vmtag"], *spec.get("tags", [])],
            "boot_disk": disk,
        },
    }
