#!/usr/bin/env python3
"""Preparación explícita de copias AlmaLinux en MNG; nunca modifica las originales.

Requiere autorización de preparación de imágenes. Estado y credenciales temporales
se conservan únicamente en un directorio privado del Control Plane.
"""

from __future__ import annotations
import argparse
import base64
import json
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
from worker.proxmox import ProxmoxClient
from scripts.prepare_almalinux_bootstrap import prepare

COPIES = {9: (29010, 29016), 10: (29011, 29017)}
BASE_PACKAGES = ("policycoreutils-python-utils", "iproute")
PROTECTED_BASE_VMIDS = {
    104,
    106,
    29000,
    29001,
    29010,
    29011,
    29012,
    29013,
    29014,
    29015,
}


class Factory:
    def __init__(self, env_file, folder, *, resume=False):
        self.env = dotenv_values(env_file, interpolate=False)
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

    def wait(self, task, timeout=900):
        assert isinstance(task, str) and task.startswith("UPID:mng01:")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.client.get(
                "/nodes/mng01/tasks/" + quote(task, safe="") + "/status"
            )
            if state.get("status") == "stopped":
                if state.get("exitstatus") != "OK":
                    raise RuntimeError("FACTORY_TASK_FAILED")
                return
            time.sleep(2)
        raise RuntimeError("FACTORY_TASK_TIMEOUT")

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
            shlex.join(argv),
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
        self.idle()
        source, vmid = COPIES[version]
        assert source != vmid and vmid not in PROTECTED_BASE_VMIDS
        assert int(self.client.get("/cluster/nextid?vmid=" + str(vmid))) == vmid
        original = self.client.vm_config("mng01", source)
        public = {
            key: original.get(key)
            for key in ["name", "template", "tags", "net0", "scsi0", "serial0"]
        }
        assert public["template"] == 1
        item = {
            "vmid": vmid,
            "source": source,
            "name": f"iacp-almalinux-{version}-bootstrap-v3-20261117",
            "phase": "clone_requested",
            "source_public": public,
        }
        self.state["copies"][str(version)] = item
        self.save()
        self.event("clonar_copia", vmid)
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
        item["clone_task"] = task
        self.save()
        self.wait(task)
        current = self.client.vm_config("mng01", vmid)
        assert current["name"] == item["name"] and not current.get("template")
        mac = current["net0"].split(",")[0].split("=", 1)[1].lower()
        item["mac"] = mac
        password = secrets.token_urlsafe(40) + "Aa9!"
        private = self.folder / ("console-password-" + str(vmid))
        private.write_text(password)
        private.chmod(0o600)
        net = ("e1000" if version == 9 else "virtio") + "=" + mac + ",bridge=vmbr0"
        self.client.put(
            "/nodes/mng01/qemu/" + str(vmid) + "/config",
            {
                "ciuser": "root",
                "cipassword": password,
                "sshkeys": quote(self.public, safe=""),
                "ipconfig0": "ip=dhcp",
                "net0": net,
                "nameserver": "1.1.1.1 8.8.8.8",
                "vga": "serial0",
                "description": "Copia autorizada para bootstrap; original "
                + str(source)
                + " intacta",
                "tags": f"almalinux-{version};iacp-guest-bootstrap-1",
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
        source, vmid = COPIES[version]
        item = self.state["copies"][str(version)]
        assert item["vmid"] == vmid and item["source"] == source
        assert item["phase"] == "ssh_pinned", "FACTORY_PHASE_NOT_RESUMABLE"
        config = self.client.vm_config("mng01", vmid)
        assert config.get("name") == item["name"] and not config.get("template")
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
        source, vmid = COPIES[version]
        mac = item["mac"]
        public = item["source_public"]
        known = self.folder / ("known-" + str(vmid))
        hostkey = " ".join(known.read_text().strip().split()[1:])
        private = self.folder / ("console-password-" + str(vmid))
        self.event("instalar_bootstrap_fijo", vmid)
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
        after = self.client.vm_config("mng01", source)
        assert {key: after.get(key) for key in public} == public, "SOURCE_IMAGE_CHANGED"
        item["phase"] = "template_ready"
        item["helper_sha256"] = json.loads((self.bundle / "manifest.json").read_text())["helper_sha256"]
        self.state.pop("result", None)
        self.state.pop("error_type", None)
        self.save()
        private.unlink()
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
                    "source": COPIES[args.version][0],
                    "copy": COPIES[args.version][1],
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
        factory.save()
        print(
            json.dumps({"result": "BLOCKED", "error_type": type(error).__name__}),
            flush=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
