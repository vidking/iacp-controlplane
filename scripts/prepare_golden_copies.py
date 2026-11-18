#!/usr/bin/env python3
"""Preparación explícita de copias AlmaLinux en MNG; nunca modifica las originales.

Requiere autorización de preparación de imágenes. Estado y credenciales temporales
se conservan únicamente en un directorio privado del Control Plane.
"""

from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import time
import uuid
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from worker.proxmox import ProxmoxClient, ProxmoxError
from scripts.prepare_almalinux_bootstrap import prepare

COPIES = {
    9: {"source": 29016, "target": 29010},
    10: {"source": 29017, "target": 29011},
}
BASE_PACKAGES = ("policycoreutils-python-utils", "iproute")
PROTECTED_BASE_VMIDS = {
    104,
    106,
    29000,
    29001,
    29012,
    29013,
    29014,
    29015,
}
REFERENCE_VM_ID = 29000
REFERENCE_NAME = "iacp-ubuntu-2404-golden"
GOLDEN_HARDWARE = {
    "bios": "ovmf",
    "machine": "q35",
    "scsihw": "virtio-scsi-single",
    "cpu": "x86-64-v4,flags=+aes",
    "cores": 10,
    "sockets": 1,
    "vcpus": 2,
    "numa": 1,
    "memory": "4096",
    "agent": "enabled=1",
    "boot": "order=scsi0",
    "serial0": "socket",
    "hotplug": "disk,network,usb,memory,cpu",
    "ipconfig0": "ip=dhcp",
}


def _device_options(value):
    if not isinstance(value, str) or not value:
        return None
    fields = value.split(",")
    result = {}
    for field in fields[1:]:
        key, separator, item = field.partition("=")
        result[key] = item if separator else True
    return result


def hardware_signature(config):
    """Return hardware-only settings, excluding per-VM volume IDs and MACs."""
    net = config.get("net0", "").split(",")
    model, separator, _mac = net[0].partition("=") if net else ("", "", "")
    net_options = {}
    for field in net[1:]:
        key, separator, value = field.partition("=")
        net_options[key] = value if separator else True
    scsi1_options = _device_options(config.get("scsi1")) or {}
    scsi0_options = _device_options(config.get("scsi0")) or {}
    efidisk_options = _device_options(config.get("efidisk0")) or {}
    scsi1_options.pop("size", None)
    return {
        **{key: str(config.get(key)) if config.get(key) is not None else None
           for key in ("bios", "machine", "scsihw", "cpu", "cores", "sockets",
                       "vcpus", "numa", "memory", "agent", "boot", "serial0",
                       "hotplug", "ipconfig0")},
        "scsi0_options": scsi0_options,
        "scsi1_options": scsi1_options,
        "efidisk_options": efidisk_options,
        "network_model": model if separator else None,
        "network_options": net_options,
        "ide_devices": sorted(key for key in config if re.fullmatch(r"ide[0-3]", key)),
        "vga": config.get("vga"),
    }


def assert_hardware_parity(reference, candidate):
    expected = hardware_signature(reference)
    actual = hardware_signature(candidate)
    if expected != actual:
        mismatches = sorted(key for key in expected if expected[key] != actual.get(key))
        raise RuntimeError("GOLDEN_HARDWARE_MISMATCH:" + ",".join(mismatches))
    if reference.get("name") != REFERENCE_NAME:
        raise RuntimeError("GOLDEN_REFERENCE_IDENTITY_MISMATCH")
    if any(expected.get(key) != str(value) for key, value in GOLDEN_HARDWARE.items()):
        raise RuntimeError("GOLDEN_REFERENCE_HARDWARE_UNEXPECTED")
    if (
        expected["network_model"] != "virtio"
        or expected["network_options"] != {"bridge": "vmbr0"}
        or expected["scsi0_options"]
        != {"discard": "on", "iothread": "1", "size": "20G"}
        or expected["scsi1_options"] != {"media": "cdrom"}
        or expected["efidisk_options"]
        != {"efitype": "4m", "pre-enrolled-keys": "0", "size": "1M"}
        or expected["ide_devices"]
        or expected["vga"] is not None
    ):
        raise RuntimeError("GOLDEN_REFERENCE_HARDWARE_UNEXPECTED")
    return expected


def _disk_size_gb(value):
    options = _device_options(value) or {}
    size = str(options.get("size", ""))
    match = re.fullmatch(r"(\d+(?:\.\d+)?)G", size, re.IGNORECASE)
    return float(match.group(1)) if match else None


def remote_command(argv):
    """Quote each SSH command argument once for the remote POSIX shell."""
    return shlex.join([str(argument) for argument in argv])


def align_hardware(client, vmid, name, reference):
    config = client.vm_config("mng01", vmid)
    if config.get("name") != name or config.get("template", 0):
        raise RuntimeError("GOLDEN_CANDIDATE_IDENTITY_MISMATCH")
    current = client.get(f"/nodes/mng01/qemu/{vmid}/status/current")
    if current.get("status") != "stopped":
        raise RuntimeError("GOLDEN_CANDIDATE_MUST_BE_STOPPED")
    root_disk = config.get("scsi0")
    if not isinstance(root_disk, str) or not root_disk.startswith(f"DS_CYBERFUEL:vm-{vmid}-disk-"):
        raise RuntimeError("GOLDEN_CANDIDATE_DISK_IDENTITY_MISMATCH")
    if _disk_size_gb(root_disk) != 20:
        size = _disk_size_gb(root_disk)
        if size is None or size > 20:
            raise RuntimeError("GOLDEN_CANDIDATE_DISK_SIZE_UNSUPPORTED")
        # Proxmox exposes QEMU disk resize as PUT. POST returns HTTP 501
        # "Method not implemented" and leaves the disk unchanged.
        task = client.put(
            f"/nodes/mng01/qemu/{vmid}/resize",
            {"disk": "scsi0", "size": f"+{20 - int(size)}G"},
        )
        if isinstance(task, str):
            wait_task(client, task)
        config = client.vm_config("mng01", vmid)

    disk_volume = root_disk.split(",", 1)[0]
    config_path = f"/nodes/mng01/qemu/{vmid}/config"
    # PVE refuses to attach its generated cloud-init volume on SCSI while the
    # cloned IDE cloud-init device still occupies the VM. Remove that device
    # first, then allocate a fresh cloud-init disk on SCSI.
    if "ide2" in config:
        try:
            client.put(config_path, {"delete": "ide2"})
        except ProxmoxError as error:
            observed = client.vm_config("mng01", vmid)
            if "ide2" in observed or not error.uncertain:
                raise
        config = client.vm_config("mng01", vmid)
    if not isinstance(config.get("scsi1"), str):
        try:
            client.put(config_path, {"scsi1": "DS_CYBERFUEL:cloudinit,media=cdrom"})
        except ProxmoxError as error:
            observed = client.vm_config("mng01", vmid)
            if (
                not isinstance(observed.get("scsi1"), str)
                or "cloudinit" not in observed["scsi1"]
                or not error.uncertain
            ):
                raise
        config = client.vm_config("mng01", vmid)
    cloudinit = config.get("scsi1")
    if not isinstance(cloudinit, str) or "cloudinit" not in cloudinit:
        raise RuntimeError("GOLDEN_CLOUDINIT_VOLUME_MISSING")
    cloudinit_volume = cloudinit.split(",", 1)[0]
    efidisk = config.get("efidisk0")
    if isinstance(efidisk, str):
        efi_options = _device_options(efidisk) or {}
        if (
            efi_options.get("efitype") != "4m"
            or efi_options.get("pre-enrolled-keys") != "0"
        ):
            efi_volume = efidisk.split(",", 1)[0]
            efidisk = efi_volume + ",efitype=4m,pre-enrolled-keys=0"
    else:
        efidisk = "DS_CYBERFUEL:1,efitype=4m,pre-enrolled-keys=0"
    nic_fields = config.get("net0", "").split(",")
    nic = nic_fields[0]
    if "=" not in nic:
        raise RuntimeError("GOLDEN_NIC_CONFIG_INVALID")
    mac = nic.split("=", 1)[1]
    ide_delete = [key for key in config if re.fullmatch(r"ide[0-3]", key)]
    if config.get("vga") is not None:
        ide_delete.append("vga")
    parameters = {
        key: value
        for key, value in GOLDEN_HARDWARE.items()
        if str(config.get(key)) != str(value)
    }
    if config.get("name") != name:
        parameters["name"] = name
    desired_tags = {
        "golden-image",
        "iacp",
        f"almalinux-{9 if vmid == 29010 else 10}",
        "v20261118",
    }
    current_tags = set(filter(None, str(config.get("tags", "")).split(";")))
    if current_tags != desired_tags:
        parameters["tags"] = ";".join(sorted(desired_tags))
    description = "Golden Image AlmaLinux alineada con el hardware Ubuntu 29000"
    if config.get("description") != description:
        parameters["description"] = description

    root_options = _device_options(config.get("scsi0")) or {}
    if disk_volume != config.get("scsi0", "").split(",", 1)[0] or {
        key: root_options.get(key) for key in ("discard", "iothread", "size")
    } != {"discard": "on", "iothread": "1", "size": "20G"}:
        parameters["scsi0"] = disk_volume + ",discard=on,iothread=1"
    cloudinit_options = _device_options(config.get("scsi1")) or {}
    if (
        config.get("scsi1", "").split(",", 1)[0] != cloudinit_volume
        or cloudinit_options.get("media") != "cdrom"
    ):
        parameters["scsi1"] = cloudinit_volume + ",media=cdrom"
    efi_options = _device_options(config.get("efidisk0")) or {}
    if (
        not isinstance(config.get("efidisk0"), str)
        or efi_options.get("efitype") != "4m"
        or efi_options.get("pre-enrolled-keys") != "0"
    ):
        parameters["efidisk0"] = efidisk
    desired_net = f"virtio={mac},bridge=vmbr0"
    if config.get("net0") != desired_net:
        parameters["net0"] = desired_net
    if ide_delete:
        parameters["delete"] = ",".join(ide_delete)
    if parameters:
        try:
            client.put(config_path, parameters)
        except ProxmoxError as error:
            if not error.uncertain:
                raise
            observed = client.vm_config("mng01", vmid)
            assert_hardware_parity(reference, observed)
            return observed
    aligned = client.vm_config("mng01", vmid)
    assert_hardware_parity(reference, aligned)
    return aligned


def wait_task(client, task, timeout=900):
    if not isinstance(task, str) or not task.startswith("UPID:mng01:"):
        raise RuntimeError("GOLDEN_TASK_ID_INVALID")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/nodes/mng01/tasks/" + quote(task, safe="") + "/status")
        if state.get("status") == "stopped":
            if state.get("exitstatus") != "OK":
                raise RuntimeError("GOLDEN_TASK_FAILED")
            return
        time.sleep(2)
    raise RuntimeError("GOLDEN_TASK_TIMEOUT")


class Factory:
    def __init__(self, env_file, folder, *, resume=False):
        self.env = dotenv_values(env_file, interpolate=False)
        for key in (
            "IACP_RUNTIME_STAGE",
            "IACP_PROXMOX_MNG_HOST_IP",
            "IACP_PROXMOX_MNG_ENDPOINT",
            "IACP_PROXMOX_MNG_API_TOKEN",
            "IACP_PROXMOX_MNG_POOL_ID",
            "IACP_API_TOKEN",
        ):
            if os.environ.get(key):
                self.env[key] = os.environ[key]
        if (
            self.env.get("IACP_RUNTIME_STAGE") != "develop"
            or self.env.get("IACP_PROXMOX_MNG_HOST_IP") != "192.168.1.113"
        ):
            raise ValueError("La fábrica solo admite dev y el nodo MNG verificado")
        self.folder = folder.resolve()
        if resume:
            if not self.folder.is_dir() or self.folder.stat().st_mode & 0o777 != 0o700:
                raise ValueError("El estado de recuperación debe ser privado")
        else:
            self.folder.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.key = self.folder / "factory_key"
        if not resume:
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)],
                check=True,
            )
        self.public = self.key.with_suffix(".pub").read_text().strip()
        self.bundle = prepare(self.folder / ("bootstrap-recovery-" + uuid.uuid4().hex if resume else "bootstrap"))
        original = socket.getaddrinfo
        socket.getaddrinfo = lambda host, *a, **kw: original(
            self.env["IACP_PROXMOX_MNG_HOST_IP"]
            if host == "mng01.cyberfuel.com"
            else host,
            *a,
            **kw,
        )
        self.client = ProxmoxClient(
            self.env["IACP_PROXMOX_MNG_ENDPOINT"],
            self.env["IACP_PROXMOX_MNG_API_TOKEN"],
        )
        self.state = json.loads((self.folder / "state.json").read_text()) if resume else {
            "purpose": "Copias de imágenes; no matriz de certificación",
            "copies": {},
        }
        self.save()

    def save(self):
        path = self.folder / "state.json"
        path.write_text(json.dumps(self.state, indent=2) + "\n")
        path.chmod(0o600)

    def event(self, phase, vmid):
        print(json.dumps({"phase": phase, "vmid": vmid}), flush=True)

    def set_phase(self, phase, vmid):
        self.state["current_phase"] = phase
        self.save()
        self.event(phase, vmid)

    def idle(self):
        request = Request(
            "https://dev-iac.cyberfuel.com/v1/observability",
            headers={"Authorization": "Bearer " + self.env["IACP_API_TOKEN"]},
        )
        with urlopen(request, timeout=30) as f:
            state = json.load(f)
        assert (
            state["active_operations"] == 0
            and state["queue_depth"] == 0
            and state["uncertain_operations"] == 0
        )
        assert len(state["workers"]) == 6 and all(
            w["status"] == "idle" and w["liveness"] == "healthy"
            for w in state["workers"]
        )
        service_request = Request(
            "https://dev-iac.cyberfuel.com/v1/service-workers",
            headers={"Authorization": "Bearer " + self.env["IACP_API_TOKEN"]},
        )
        with urlopen(service_request, timeout=30) as f:
            service_state = json.load(f)
        assert len(service_state["workers"]) == 4 and all(
            w["status"] == "idle" and w["job_id"] is None
            for w in service_state["workers"]
        )

    def wait(self, task, timeout=900):
        return wait_task(self.client, task, timeout)

    def ssh(self, item, argv, input=None, check=True):
        args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + str(self.folder / ("known-" + str(item["vmid"]))),
            "-o",
            "ConnectTimeout=15",
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            str(self.key),
            "root@" + item["address"],
            remote_command(argv),
        ]
        r = subprocess.run(
            args, input=input, capture_output=True, text=True, timeout=600
        )
        if check and r.returncode:
            raise RuntimeError("FACTORY_SSH_COMMAND_FAILED")
        return r

    def addresses(self, vmid, mac):
        for _ in range(90):
            try:
                values = [
                    a["ip-address"]
                    for interface in self.client.guest_agent_interfaces("mng01", vmid)
                    if interface.get("hardware-address", "").lower() == mac
                    for a in interface.get("ip-addresses", [])
                    if a.get("ip-address-type") == "ipv4"
                ]
                if len(set(values)) == 1:
                    return values[0]
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("FACTORY_DHCP_NOT_READY")

    def console_key(self, item, password):
        from websockets.sync.client import connect

        proxy = self.client.post(
            "/nodes/mng01/qemu/" + str(item["vmid"]) + "/termproxy",
            {"serial": "serial0"},
        )
        uri = (
            "wss://mng01.cyberfuel.com:8006/api2/json/nodes/mng01/qemu/"
            + str(item["vmid"])
            + "/vncwebsocket?"
            + urlencode({"port": proxy["port"], "vncticket": proxy["ticket"]})
        )
        headers = {
            "Authorization": "PVEAPIToken=" + self.env["IACP_PROXMOX_MNG_API_TOKEN"]
        }
        with connect(
            uri,
            ssl=ssl.create_default_context(),
            subprotocols=["binary"],
            additional_headers=headers,
            proxy=None,
            open_timeout=20,
        ) as ws:
            ws.send(proxy["user"] + ":" + proxy["ticket"] + "\n")
            first = ws.recv(timeout=20)
            first = first.encode() if isinstance(first, str) else first
            if not first.startswith(b"OK"):
                raise RuntimeError("FACTORY_CONSOLE_AUTH_FAILED")

            def send(value):
                ws.send("0:" + str(len(value.encode())) + ":" + value)

            def expect(pattern, timeout=120):
                data = b""
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        value = ws.recv(
                            timeout=min(10, max(0.1, deadline - time.monotonic()))
                        )
                    except TimeoutError:
                        ws.send("2")
                        continue
                    data += value.encode() if isinstance(value, str) else value
                    match = re.search(pattern, data)
                    if match:
                        return match
                    data = data[-65536:]
                raise RuntimeError("FACTORY_CONSOLE_PROMPT_TIMEOUT")

            send("\r")
            expect(rb"login:\s*$")
            send("root\r")
            expect(rb"[Pp]assword:\s*$")
            send(password + "\r")
            expect(rb"[#]\s")
            send("stty -echo\r")
            nonce = uuid.uuid4().hex
            send(
                "printf 'IACP_"
                + nonce
                + ":'; base64 -w0 /etc/ssh/ssh_host_ed25519_key.pub; printf ':END_"
                + nonce
                + "\\n'\r"
            )
            match = expect(
                ("IACP_" + nonce + ":([A-Za-z0-9+/=]+):END_" + nonce).encode(), 30
            )
            key = base64.b64decode(match.group(1)).decode().split()
            if len(key) < 2 or key[0] != "ssh-ed25519":
                raise RuntimeError("FACTORY_HOST_KEY_INVALID")
            send("stty echo; exit\r")
            return " ".join(key[:2])

    def prepare_copy(self, version):
        source = COPIES[version]["source"]
        vmid = COPIES[version]["target"]
        self.set_phase("comprobar_capacidad", vmid)
        self.idle()
        assert source != vmid and vmid not in PROTECTED_BASE_VMIDS
        self.set_phase("leer_referencia_ubuntu", vmid)
        reference = self.client.vm_config("mng01", REFERENCE_VM_ID)
        if reference.get("template") != 1 or reference.get("name") != REFERENCE_NAME:
            raise RuntimeError("GOLDEN_REFERENCE_NOT_PROTECTED_TEMPLATE")
        self.set_phase("leer_fuente_almalinux", vmid)
        original = self.client.vm_config("mng01", source)
        if original.get("template") != 1:
            raise RuntimeError("GOLDEN_ALMALINUX_SOURCE_NOT_TEMPLATE")
        public = {
            key: original.get(key)
            for key in ["name", "template", "tags", "net0", "scsi0", "serial0"]
        }
        item = {
            "vmid": vmid,
            "source": source,
            "name": f"iacp-almalinux-{version}-golden",
            "phase": "clone_requested",
            "source_public": public,
        }
        self.state["copies"][str(version)] = item
        self.save()
        self.set_phase("comprobar_inventario", vmid)
        inventory = {int(row["vmid"]): row for row in self.client.inventory()}
        target = inventory.get(vmid)
        if target is None:
            self.set_phase("reservar_vmid_objetivo", vmid)
            if int(self.client.get("/cluster/nextid?vmid=" + str(vmid))) != vmid:
                raise RuntimeError("GOLDEN_TARGET_VMID_NOT_FREE")
            self.set_phase("crear_copia_completa", vmid)
            try:
                task = self.client.post(
                    "/nodes/mng01/qemu/" + str(source) + "/clone",
                    {
                        "newid": vmid,
                        "full": 1,
                        "name": item["name"],
                        "target": "mng01",
                        "storage": "DS_CYBERFUEL",
                        "pool": self.env["IACP_PROXMOX_MNG_POOL_ID"],
                    },
                )
            except ProxmoxError:
                # A timeout can lose the POST response after Proxmox accepted
                # the clone. Observe the target before any retry.
                observed = {int(row["vmid"]): row for row in self.client.inventory()}.get(vmid)
                if observed is None or observed.get("name") != item["name"]:
                    raise
                task = None
            item["clone_task"] = task
            self.save()
            if task:
                self.wait(task)
        else:
            if (
                target.get("name") != item["name"]
                or target.get("node") != "mng01"
                or target.get("pool") != self.env["IACP_PROXMOX_MNG_POOL_ID"]
                or target.get("template")
                or target.get("status") != "stopped"
            ):
                raise RuntimeError("GOLDEN_EXISTING_TARGET_NOT_SAFE_TO_REUSE")
        current = self.client.vm_config("mng01", vmid)
        if current.get("name") != item["name"] or current.get("template"):
            raise RuntimeError("GOLDEN_CLONE_IDENTITY_MISMATCH")
        mac = current["net0"].split(",")[0].split("=", 1)[1].lower()
        item["mac"] = mac
        self.set_phase("alinear_hardware_con_ubuntu_29000", vmid)
        align_hardware(self.client, vmid, item["name"], reference)
        item["hardware_signature"] = hardware_signature(reference)
        item["phase"] = "hardware_aligned"
        self.save()
        password = secrets.token_urlsafe(40) + "Aa9!"
        private = self.folder / ("console-password-" + str(vmid))
        private.write_text(password)
        private.chmod(0o600)
        self.set_phase("configurar_acceso_cloudinit", vmid)
        self.client.put(
            "/nodes/mng01/qemu/" + str(vmid) + "/config",
            {
                "ciuser": "root",
                "cipassword": password,
                # ProxmoxClient encodes this plain OpenSSH key at the HTTP
                # boundary; callers must never pre-quote it.
                "sshkeys": self.public,
                "ipconfig0": "ip=dhcp",
                "nameserver": "1.1.1.1 8.8.8.8",
            },
        )
        item["phase"] = "configured"
        self.save()
        self.wait(self.client.post("/nodes/mng01/qemu/" + str(vmid) + "/status/start"))
        self.event("validar_consola_confiable", vmid)
        hostkey = self.console_key(item, password)
        item["address"] = self.addresses(vmid, mac)
        known = self.folder / ("known-" + str(vmid))
        known.write_text(item["address"] + " " + hostkey + "\n")
        known.chmod(0o600)
        assert self.ssh(item, ["getenforce"]).stdout.strip() == "Enforcing"
        item["phase"] = "ssh_pinned"
        self.save()
        self.finish_copy(version, item)

    def resume_copy(self, version):
        self.idle()
        source = COPIES[version]["source"]
        vmid = COPIES[version]["target"]
        item = self.state["copies"][str(version)]
        assert item["vmid"] == vmid and item["source"] == source
        assert item["phase"] == "ssh_pinned", "FACTORY_PHASE_NOT_RESUMABLE"
        config = self.client.vm_config("mng01", vmid)
        assert config.get("name") == item["name"] and not config.get("template")
        assert_hardware_parity(
            self.client.vm_config("mng01", REFERENCE_VM_ID), config
        )
        assert config["net0"].split(",")[0].split("=", 1)[1].lower() == item["mac"]
        original = self.client.vm_config("mng01", source)
        assert {k: original.get(k) for k in item["source_public"]} == item["source_public"]
        if item.get("helper_pid") and "helper_exitcode" not in item:
            assert self.client.guest_agent_exec_status("mng01", vmid, item["helper_pid"]).get("exited"), "FACTORY_PREVIOUS_HELPER_RUNNING"
        item["address"] = self.addresses(vmid, item["mac"])
        known = self.folder / ("known-" + str(vmid))
        hostkey = " ".join(known.read_text().strip().split()[1:])
        assert hostkey.startswith("ssh-ed25519 ")
        known.write_text(item["address"] + " " + hostkey + "\n")
        assert self.ssh(item, ["getenforce"]).stdout.strip() == "Enforcing"
        self.finish_copy(version, item)

    def finish_copy(self, version, item):
        source = COPIES[version]["source"]
        vmid = COPIES[version]["target"]
        mac = item["mac"]
        public = item["source_public"]
        known = self.folder / ("known-" + str(vmid))
        hostkey = " ".join(known.read_text().strip().split()[1:])
        private = self.folder / ("console-password-" + str(vmid))
        self.event("instalar_bootstrap_fijo", vmid)
        self.ssh(item, ["test", "-d", "/sys/firmware/efi"])
        self.ssh(item, ["systemctl", "is-active", "qemu-guest-agent"])
        self.ssh(item, ["test", "-e", "/sys/bus/pci/drivers/virtio-pci"])
        self.ssh(item, ["test", "-e", "/sys/bus/scsi/devices"])
        self.ssh(item, ["dnf", "-y", "install", *BASE_PACKAGES])
        # El bootstrap y la prevalidación WAN consultan interfaces, rutas y
        # vecinos mediante iproute2. No activar esta copia en el catálogo si
        # el comando no quedó disponible dentro del guest.
        self.ssh(item, ["ip", "-V"])
        self.ssh(item, ["mkdir", "-p", "/root/iacp-bootstrap-package"])
        for name in ["iacp-guest-bootstrap", "manifest.json", "install.sh"]:
            content = (self.bundle / name).read_text()
            self.ssh(
                item,
                [
                    "sh",
                    "-c",
                    "umask 077; cat > "
                    + shlex.quote("/root/iacp-bootstrap-package/" + name),
                ],
                input=content,
            )
        self.ssh(item, ["bash", "/root/iacp-bootstrap-package/install.sh"])
        identifier = item.get("helper_identifier") or str(uuid.uuid4())
        item["helper_identifier"] = identifier
        self.save()
        result = self.client.guest_agent_exec(
            "mng01",
            vmid,
            [
                "/usr/local/libexec/iacp-guest-bootstrap",
                identifier,
                mac,
                "dhcp",
                "",
                "",
            ],
        )
        item["helper_pid"] = result["pid"]
        item.pop("helper_exitcode", None)
        self.save()
        for _ in range(120):
            status = self.client.guest_agent_exec_status("mng01", vmid, result["pid"])
            if status.get("exited"):
                break
            time.sleep(2)
        if status.get("exited"):
            item["helper_exitcode"] = status.get("exitcode")
            self.save()
        assert status.get("exited") and status.get("exitcode") == 0, (
            "FACTORY_HELPER_FAILED"
        )
        observed = json.loads(status.get("out-data", "{}"))
        assert (
            observed.get("bootstrap_protocol") == 1
            and observed.get("instance_id") == identifier
            and observed.get("mac_address") == mac
        )
        assert observed.get("host_key") == hostkey and observed.get("dns_servers") == [
            "1.1.1.1",
            "8.8.8.8",
        ]
        item["address"] = self.addresses(vmid, mac)
        known.write_text(item["address"] + " " + hostkey + "\n")
        assert self.ssh(item, ["getenforce"]).stdout.strip() == "Enforcing"
        item["phase"] = "helper_verified"
        self.save()
        self.event("helper_verificado_selinux_enforcing", vmid)
        # Una sola sesión confiable: retirar credenciales de fábrica antes de apagar.
        cleanup = "set -e; rm -rf /var/lib/iacp /root/iacp-bootstrap-package; rm -f /root/.ssh/authorized_keys; passwd -l root >/dev/null; cloud-init clean --logs --machine-id; rm -f /etc/ssh/ssh_host_* /var/lib/NetworkManager/*lease* /var/lib/NetworkManager/secret_key; printf 'IACP_FACTORY_CLEAN_OK\\n'; systemctl poweroff"
        cleaned = self.ssh(item, ["sh", "-c", cleanup], check=False)
        assert "IACP_FACTORY_CLEAN_OK" in cleaned.stdout, "FACTORY_CLEANUP_UNCONFIRMED"
        for _ in range(90):
            state = self.client.get(
                "/nodes/mng01/qemu/" + str(vmid) + "/status/current"
            )
            if state.get("status") == "stopped":
                break
            time.sleep(2)
        assert state.get("status") == "stopped"
        self.client.put(
            "/nodes/mng01/qemu/" + str(vmid) + "/config",
            {"delete": "cipassword,sshkeys"},
        )
        task = self.client.post("/nodes/mng01/qemu/" + str(vmid) + "/template")
        if isinstance(task, str):
            self.wait(task)
        template = self.client.vm_config("mng01", vmid)
        assert template.get("template") == 1
        reference = self.client.vm_config("mng01", REFERENCE_VM_ID)
        assert_hardware_parity(reference, template)
        after = self.client.vm_config("mng01", source)
        assert {key: after.get(key) for key in public} == public, "SOURCE_IMAGE_CHANGED"
        item["phase"] = "template_ready"
        item["helper_sha256"] = json.loads((self.bundle / "manifest.json").read_text())["helper_sha256"]
        self.state.pop("result", None)
        self.state.pop("error_type", None)
        self.save()
        private.unlink()
        self.key.unlink(missing_ok=True)
        self.key.with_suffix(".pub").unlink(missing_ok=True)
        known.unlink(missing_ok=True)
        self.event("copia_versionada_lista", vmid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path("/etc/iacp/stacks/dev.env"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--version", type=int, choices=[9, 10], required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Recuperar solamente una copia con SSH ya fijado y proceso anterior terminado")
    args = parser.parse_args()
    if not args.execute:
        print(
            json.dumps(
                {
                    "source": COPIES[args.version]["source"],
                    "copy": COPIES[args.version]["target"],
                    "node": "mng01",
                    "mutations": False,
                }
            )
        )
        return
    factory = Factory(args.env, args.state_dir, resume=args.resume)
    try:
        if args.resume:
            factory.resume_copy(args.version)
        else:
            factory.prepare_copy(args.version)
    except Exception as error:
        factory.state["result"] = "BLOCKED"
        factory.state["error_type"] = type(error).__name__
        failure = {
            "result": "BLOCKED",
            "error_type": type(error).__name__,
            "phase": factory.state.get("current_phase"),
        }
        if isinstance(error, ProxmoxError):
            factory.state["error_code"] = str(error)
            factory.state["http_status"] = error.status
            factory.state["uncertain"] = error.uncertain
            failure.update(
                error_code=str(error),
                http_status=error.status,
                uncertain=error.uncertain,
            )
        factory.save()
        print(json.dumps(failure), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
