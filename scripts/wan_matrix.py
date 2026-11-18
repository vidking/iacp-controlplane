#!/usr/bin/env python3
"""Matriz de red del piloto dev: seis inyecciones y contención ante incertidumbre.

Ejecutar en el Control Plane con su entorno privado, nunca contra producción.
La evidencia no contiene credenciales ni salidas de aplicación sin filtrar.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from worker.proxmox import ProxmoxClient

WAN_BRIDGE = "vmbr3"
REQUIRED_SERVICE_WORKERS = 4


def wan_nic_config(env):
    """Validate the only authorized WAN NIC layout for this physical matrix."""

    bridge = (env.get("IACP_PROD_STATIC_BRIDGE") or "").strip()
    if bridge != WAN_BRIDGE:
        raise RuntimeError("WAN_BRIDGE_NOT_AUTHORIZED")
    if "IACP_PROD_STATIC_PROXMOX_VLAN_TAG" not in env:
        raise RuntimeError("WAN_PROXMOX_VLAN_TAG_NOT_CONFIGURED")
    vlan_tag = (env.get("IACP_PROD_STATIC_PROXMOX_VLAN_TAG") or "").strip().lower()
    if vlan_tag not in {"", "none", "untagged"}:
        raise RuntimeError("WAN_PROXMOX_TAG_NOT_AUTHORIZED")
    return WAN_BRIDGE, None


def require_four_idle_service_workers(service_state):
    workers = service_state.get("workers") or []
    if (
        service_state.get("max_concurrent") != REQUIRED_SERVICE_WORKERS
        or len(workers) != REQUIRED_SERVICE_WORKERS
    ):
        raise RuntimeError("FOUR_SERVICE_WORKERS_REQUIRED")
    now = datetime.now(UTC)
    for worker in workers:
        try:
            heartbeat = datetime.fromisoformat(worker["last_heartbeat"])
            heartbeat_age = (now - heartbeat).total_seconds()
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("SERVICE_WORKERS_NOT_IDLE") from None
        if (
            worker.get("status") != "idle"
            or worker.get("job_id")
            or not 0 <= heartbeat_age < 60
        ):
            raise RuntimeError("SERVICE_WORKERS_NOT_IDLE")


def cases(category):
    if category == "kvm":
        items = [
            ("kvm", image)
            for image in ["ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10"]
        ]
    else:
        products = (
            ["nextcloud", "n8n"] if category == "services" else ["hermes", "openclaw"]
        )
        items = [
            (product, image)
            for product in products
            for image in ["ubuntu-2404", "almalinux-9"]
        ]
    result = [
        {"product": product, "image": image, "plan": f"kvm-{plan}", "control": False}
        for product, image in items
        for plan in range(1, 5)
    ]
    controls = [dict(result[0], control=True), dict(result[-1], control=True)]
    if category == "services":
        # Controles explícitos para el contrato de acceso solo con contraseña.
        for case in controls:
            case["client_ssh_key"] = False
    return result + controls


class Matrix:
    def __init__(self, args):
        self.abort = threading.Event()
        self.api_lock = threading.Lock()
        self.last_request = 0.0
        self.args = args
        self.profile = args.network_profile
        self.env = dotenv_values(args.env, interpolate=False)
        assert self.env["IACP_RUNTIME_STAGE"] == "develop"
        assert (
            self.env["IACP_VMID_MIN"] == "40000"
            and self.env["IACP_VMID_MAX"] == "49999"
        )
        self.base = "https://dev-iac.cyberfuel.com"
        self.token = self.env["IACP_API_TOKEN"]
        assert args.cycle in (1, 2)
        self.static_bridge = WAN_BRIDGE if self.profile == "prod_static" else "vmbr0"
        self.static_vlan_tag = None
        self.folder = args.evidence.resolve()
        self.folder.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.key = self.folder / "client_key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)],
            check=True,
        )
        self.askpass = self.folder / "askpass"
        self.askpass.write_text("#!/bin/sh\nprintf '%s\\n' \"$IACP_MATRIX_PASSWORD\"\n")
        self.askpass.chmod(0o700)
        self.public = self.key.with_suffix(".pub").read_text().strip()
        self.log = self.folder / "results.jsonl"
        original = socket.getaddrinfo
        socket.getaddrinfo = lambda host, *a, **kw: original(
            self.env["IACP_PROXMOX_MNG_HOST_IP"]
            if host == "mng01.cyberfuel.com"
            else host,
            *a,
            **kw,
        )
        self.proxmox = ProxmoxClient(
            self.env["IACP_PROXMOX_MNG_ENDPOINT"],
            self.env["IACP_PROXMOX_MNG_API_TOKEN"],
        )

    def verify_network_profile(self):
        if self.profile == "prod_static":
            self.static_bridge, self.static_vlan_tag = wan_nic_config(self.env)
            profiles = self.api("GET", "/v1/network-profiles")["network_profiles"]
            wan = next(item for item in profiles if item["profile_id"] == "prod_static")
            assert wan["enabled"] is True, "WAN_PROFILE_DISABLED"
            assert wan["proxmox_bridge"] == WAN_BRIDGE, "WAN_BRIDGE_CONFIG_MISMATCH"
            assert wan["proxmox_vlan_tag"] == self.static_vlan_tag, "WAN_VLAN_TAG_CONFIG_MISMATCH"
            self.record(
                "network_profile_verified",
                profile="prod_static",
                bridge=WAN_BRIDGE,
            )

    def record(self, event, **data):
        with self.log.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "event": event,
                        **data,
                    }
                )
                + "\n"
            )
        try:
            print(json.dumps({"event": event, **data}), flush=True)
        except BrokenPipeError:
            # La evidencia persistente es autoritativa; perder SSH no debe detener
            # pruebas ni dejar recursos físicos sin la fase de limpieza.
            sys.stdout = open(os.devnull, "w")

    def api(self, method, path, body=None, key=None, raw=False):
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        }
        if key:
            headers["Idempotency-Key"] = key
        r = Request(
            self.base + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
        )
        for attempt in range(4):
            if self.abort.is_set() and method != "GET":
                raise RuntimeError("MATRIX_CONTAINMENT_ACTIVE")
            with self.api_lock:
                time.sleep(max(0, 0.25 - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
            try:
                with urlopen(r, timeout=90) as f:
                    if (
                        f.headers.get("X-IACP-Build-Revision")
                        != self.env["IACP_IMAGE_TAG"]
                    ):
                        raise RuntimeError("RUNTIME_REVISION_MISMATCH")
                    return f.read() if raw else json.load(f)
            except HTTPError as e:
                if e.code == 429 and (method == "GET" or key) and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(
                    f"API_{e.code}_{method}_{path.split('/')[2]}"
                ) from None

    def parallel(self, function, items):
        def guarded(item):
            if self.abort.is_set():
                raise RuntimeError("MATRIX_CONTAINMENT_ACTIVE")
            try:
                return function(item)
            except Exception:
                self.abort.set()
                raise

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            return list(pool.map(guarded, items))

    def idle(self):
        state = self.api("GET", "/v1/observability")
        assert (
            state["max_concurrent"] == 6
            and state["active_operations"] == 0
            and state["queue_depth"] == 0
            and state["uncertain_operations"] == 0
        ), "RUNTIME_NOT_IDLE"
        assert len(state["workers"]) == 6 and all(
            w["status"] == "idle"
            and w["liveness"] == "healthy"
            and not w["current_operation_id"]
            for w in state["workers"]
        ), "SIX_WORKERS_REQUIRED"
        service_state = self.api("GET", "/v1/service-workers")
        require_four_idle_service_workers(service_state)
        return state

    def wait_operation(self, job):
        for _ in range(450):
            r = self.api("GET", "/v1/operations/" + job["operation_id"])
            if r["status"] in {"completed", "succeeded"}:
                return r
            if r["status"] in {"failed", "uncertain"}:
                raise RuntimeError("OPERATION_" + r["status"].upper())
            time.sleep(2)
        raise RuntimeError("OPERATION_TIMEOUT")

    def qga(self, vmid, argv):
        result = self.proxmox.guest_agent_exec("mng01", vmid, argv)
        for _ in range(90):
            state = self.proxmox.guest_agent_exec_status("mng01", vmid, result["pid"])
            if state.get("exited"):
                assert state.get("exitcode") == 0, "QGA_COMMAND_FAILED"
                return state.get("out-data", "")
            time.sleep(1)
        raise RuntimeError("QGA_TIMEOUT")

    def binding(self, item):
        vmid = item["vmid"]
        config = self.proxmox.vm_config("mng01", vmid)
        nic = dict(
            field.split("=", 1)
            for field in config["net0"].split(",")
            if "=" in field
        )
        assert config["name"] == item["name"]
        if self.profile == "prod_static":
            assert nic.get("bridge") == self.static_bridge
            assert nic.get("tag") == (
                str(self.static_vlan_tag)
                if self.static_vlan_tag is not None
                else None
            )
            assert config["ipconfig0"] in [
                f"ip={item['address']}/24,gw=190.0.231.1",
                f"gw=190.0.231.1,ip={item['address']}/24",
            ]
        else:
            assert nic.get("bridge") == "vmbr0"
            assert "tag=" not in config["net0"] and config["ipconfig0"] == "ip=dhcp"
        known = self.folder / ("known-" + str(vmid))
        if item["image"] in {"almalinux-9", "almalinux-10"}:
            mac = config["net0"].split(",")[0].split("=", 1)[1].lower()
            observed = json.loads(self.qga(vmid, [
                "/usr/local/libexec/iacp-guest-bootstrap", item["instance_id"], mac,
                "static" if self.profile == "prod_static" else "dhcp",
                item["address"] + "/24" if self.profile == "prod_static" else "",
                "190.0.231.1" if self.profile == "prod_static" else "",
            ]))
            assert observed.get("bootstrap_protocol") == 1
            assert observed.get("instance_id") == item["instance_id"] and observed.get("mac_address") == mac
            assert not observed.get("ipv4") or observed["ipv4"] == item["address"]
            hostkey = observed.get("host_key", "")
        else:
            hostkey = " ".join(self.qga(vmid, ["/bin/cat", "/etc/ssh/ssh_host_ed25519_key.pub"]).split()[:2])
        assert re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", hostkey), "SSH_HOST_KEY_INVALID"
        known.write_text(item["address"] + " " + hostkey + "\n")
        item["known"] = str(known)

    def ssh(self, item, argv, input=None, password=None):
        if self.abort.is_set():
            raise RuntimeError("MATRIX_CONTAINMENT_ACTIVE")
        if not item.get("client_ssh_key", True) and password is None:
            password = item["password"]
        args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + item["known"],
            "-o",
            "ConnectTimeout=15",
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            str(self.key),
            "root@" + item["address"],
            shlex.join(argv),
        ]
        environment = os.environ.copy()
        if password is not None:
            args[args.index("BatchMode=yes")] = "BatchMode=no"
            args[1:1] = [
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
            environment.update(
                SSH_ASKPASS=str(self.askpass),
                SSH_ASKPASS_REQUIRE="force",
                DISPLAY=":iacp",
                IACP_MATRIX_PASSWORD=password,
            )
        result = subprocess.run(
            args,
            input=input,
            text=True,
            capture_output=True,
            timeout=240,
            env=environment,
            start_new_session=True,
        )
        if result.returncode:
            raise RuntimeError("SSH_COMMAND_FAILED")
        return result.stdout

    def create(self, index, case):
        label = "kvm-" + case["image"] if case["product"] == "kvm" else (
            ("service-" if self.args.category == "services" else "agent-") + case["product"]
        )
        name = f"prueba-{label}-c{self.args.cycle}-{self.args.run}-{index:02d}"
        password = secrets.token_urlsafe(32)
        body = {
            "name": name,
            "plan_id": case["plan"],
            "image_id": case["image"],
            "environment": "lab",
            "network_profile": self.profile,
            "access": {
                "ssh_public_keys": [self.public] if case.get("client_ssh_key", True) else [],
                "root_password": password,
            },
            "tags": ["wan-matrix-" + self.args.run],
            "start_on_create": True,
        }
        if case["product"] == "kvm":
            result = self.api("POST", "/v1/instances", body, name)
        else:
            config = (
                {"access_mode": "ip"}
                if case["product"] in {"nextcloud", "n8n"}
                else {"agent_mode": "local"}
            )
            result = self.api(
                "POST",
                "/v1/service-instances",
                {
                    "application_id": case["product"],
                    "instance": body,
                    "configuration": config,
                },
                name,
            )
        item = dict(case, **result, name=name, password=password)
        return item

    def web(
        self, item, path, method="GET", data=None, extra_headers=None, basic_auth=True
    ):
        context = ssl.create_default_context(cafile=str(item["ca"]))
        credentials = item["credentials"]
        headers = {}
        if basic_auth:
            user = credentials.get("admin_user")
            password = credentials.get("admin_password")
            if not user or not password:
                raise RuntimeError("WEB_CREDENTIALS_UNAVAILABLE")
            authorization = base64.b64encode(
                (user + ":" + password).encode()
            ).decode()
            headers["Authorization"] = "Basic " + authorization
        if data is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        r = Request(
            "https://" + item["address"] + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(r, context=context, timeout=60) as f:
                return f.status, f.read(), f.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def n8n_owner_auth(self, item, setup_owner):
        email = f"prueba-n8n-{item['vmid']}@example.invalid"
        if setup_owner:
            item["n8n_owner_password"] = secrets.token_urlsafe(32) + "Aa9!"
        password = item.get("n8n_owner_password")
        assert password, "N8N_OWNER_PASSWORD_NOT_PRESERVED"
        if setup_owner:
            status, body, _ = self.web(
                item,
                "/rest/owner/setup",
                "POST",
                json.dumps({
                    "email": email,
                    "firstName": "Prueba",
                    "lastName": "Servicio",
                    "password": password,
                }).encode(),
                basic_auth=False,
            )
            result = json.loads(body)
            assert status == 200 and (result.get("data") or result).get("email") == email, "N8N_OWNER_SETUP_FAILED"
        status, body, response_headers = self.web(
            item,
            "/rest/login",
            "POST",
            json.dumps({"emailOrLdapLoginId": email, "password": password}).encode(),
            basic_auth=False,
        )
        result = json.loads(body)
        assert status == 200 and (result.get("data") or result).get("email") == email, "N8N_OWNER_LOGIN_FAILED"
        cookies = response_headers.get_all("Set-Cookie", [])
        cookie = "; ".join(value.split(";", 1)[0] for value in cookies)
        assert cookie, "N8N_OWNER_SESSION_MISSING"
        if setup_owner:
            status, _, _ = self.web(
                item,
                "/rest/login",
                "POST",
                json.dumps({"emailOrLdapLoginId": email, "password": password + "X"}).encode(),
                basic_auth=False,
            )
            assert status == 401, "N8N_INVALID_PASSWORD_ACCEPTED"
        status, body, _ = self.web(
            item,
            "/rest/workflows?skip=0&take=10",
            extra_headers={"Cookie": cookie},
            basic_auth=False,
        )
        assert status == 200 and any(
            workflow.get("id") == "iacpValidation"
            for workflow in json.loads(body).get("data", [])
        ), "N8N_OWNER_WORKFLOW_ACCESS_FAILED"

    def application(self, item, after_reboot=False):
        product = item["product"]
        root = "/opt/iacp-services/" + item["service_id"]
        base = ["docker", "compose", "--project-directory", root]
        if product == "nextcloud":
            path = "/remote.php/dav/files/admin/iacp-persistence.txt"
            if not after_reboot:
                self.web(item, path, "PUT", b"iacp-persistence")
            assert self.web(item, path)[1] == b"iacp-persistence"
        elif product == "n8n":
            readiness, body, _ = self.web(
                item, "/healthz/readiness", basic_auth=False
            )
            assert readiness == 200 and json.loads(body).get("status") == "ok", "N8N_NOT_READY"
            assert self.web(item, "/", basic_auth=False)[0] == 200
            if not after_reboot:
                workflow = {
                    "id": "iacpValidation",
                    "name": "IACP validacion",
                    "nodes": [
                        {
                            "parameters": {},
                            "id": "start",
                            "name": "Start",
                            "type": "n8n-nodes-base.manualTrigger",
                            "typeVersion": 1,
                            "position": [0, 0],
                        },
                        {
                            "parameters": {
                                "jsCode": "return [{json:{iacpValidation:true}}];"
                            },
                            "id": "code",
                            "name": "Code",
                            "type": "n8n-nodes-base.code",
                            "typeVersion": 2,
                            "position": [200, 0],
                        },
                    ],
                    "connections": {
                        "Start": {
                            "main": [[{"node": "Code", "type": "main", "index": 0}]]
                        }
                    },
                    "settings": {"executionOrder": "v1"},
                    "active": False,
                }
                self.ssh(
                    item,
                    base
                    + [
                        "exec",
                        "-T",
                        "app",
                        "sh",
                        "-c",
                        "cat > /home/node/.n8n/iacp-workflow.json",
                    ],
                    input=json.dumps(workflow),
                )
                self.ssh(
                    item,
                    base
                    + [
                        "exec",
                        "-T",
                        "app",
                        "n8n",
                        "import:workflow",
                        "--input=/home/node/.n8n/iacp-workflow.json",
                    ],
                )
            output = self.ssh(
                item,
                base + ["exec", "-T", "-e", "N8N_RUNNERS_BROKER_PORT=5681", "app", "n8n", "execute", "--id=iacpValidation"],
            )
            assert "iacpValidation" in output and "true" in output, (
                "N8N_WORKFLOW_FAILED"
            )
            self.n8n_owner_auth(item, setup_owner=not after_reboot)
        elif product == "hermes":
            self.ssh(item, base + ["exec", "-T", "app", "hermes", "config", "check"])
        else:
            self.ssh(
                item, base + ["exec", "-T", "app", "node", "openclaw.mjs", "health"]
            )
        states = (
            json.loads(self.ssh(item, base + ["ps", "--format", "json"]))
            if product in {"hermes", "openclaw"}
            else None
        )
        if states is not None:
            states = states if isinstance(states, list) else [states]
            assert states and all(s["State"] == "running" for s in states)

    def verify(self, item):
        instance = self.api("GET", "/v1/instances/" + item["instance_id"])
        item["vmid"] = instance["vmid"]
        assert 40000 <= item["vmid"] <= 49999
        allocation = instance["network_allocation"]
        if self.profile == "prod_static":
            assert (
                allocation["status"] == "allocated"
                and allocation["gateway"] == "190.0.231.1"
            )
            item["address"] = allocation["address"].split("/")[0]
            assert item["address"].startswith("190.0.231.")
        else:
            assert allocation is None
            config = self.proxmox.vm_config("mng01", item["vmid"])
            mac = config["net0"].split(",")[0].split("=")[1].lower()
            addresses = [
                entry["ip-address"]
                for interface in self.proxmox.guest_agent_interfaces(
                    "mng01", item["vmid"]
                )
                if interface.get("hardware-address", "").lower() == mac
                for entry in interface.get("ip-addresses", [])
                if entry.get("ip-address-type") == "ipv4"
            ]
            assert len(addresses) == 1, "DHCP_ADDRESS_AMBIGUOUS"
            item["address"] = addresses[0]
        self.binding(item)
        assert self.ssh(item, ["hostname"]).strip() == item["name"]
        assert (
            self.ssh(item, ["hostname"], password=item["password"]).strip()
            == item["name"]
        )
        self.ssh(
            item,
            [
                "sh",
                "-c",
                "set -e; ip route show default | grep -q default; getent hosts example.com >/dev/null; curl --fail --silent --show-error --max-time 30 https://example.com/ >/dev/null",
            ],
        )
        self.ssh(
            item, ["sh", "-c", "printf iacp-persistence > /root/iacp-validation.txt"]
        )
        if item["product"] != "kvm":
            item["credentials"] = self.api(
                "POST", "/v1/service-instances/" + item["service_id"] + "/credentials"
            )
            if item["product"] in {"nextcloud", "n8n"}:
                ca = self.folder / ("ca-" + str(item["vmid"]) + ".crt")
                ca.write_bytes(
                    self.api(
                        "GET",
                        "/v1/service-instances/" + item["service_id"] + "/tls-ca",
                        raw=True,
                    )
                )
                item["ca"] = ca
            self.application(item)
            delivery = self.api(
                "POST",
                "/v1/service-instances/" + item["service_id"] + "/deliver",
                key="delivery-" + item["name"],
            )
            self.wait_service(delivery["job_id"])
        self.wait_operation(
            self.api(
                "POST",
                "/v1/instances/" + item["instance_id"] + "/power/reboot",
                key="reboot-" + item["name"],
            )
        )
        for attempt in range(60):
            try:
                assert (
                    self.ssh(item, ["cat", "/root/iacp-validation.txt"])
                    == "iacp-persistence"
                )
                if item["product"] != "kvm":
                    self.application(item, True)
                break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(3)
        self.record(
            "verified",
            name=item["name"],
            vmid=item["vmid"],
            address=item["address"],
            product=item["product"],
            image=item["image"],
            plan=item["plan"],
            control=item["control"],
        )

    def wait_service(self, jobid):
        retry_attempted = False
        retryable_errors = {
            "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_APPLICATION_IMAGE_PULL",
            "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_FAILED_APPLICATION_START",
            "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_UNREACHABLE_APPLICATION_START",
        }
        for _ in range(1200):
            job = self.api("GET", "/v1/service-jobs/" + jobid)
            if job["status"] == "succeeded":
                return
            if (
                not retry_attempted
                and job["status"] in {"failed", "uncertain"}
                and job.get("error") in retryable_errors
            ):
                worker_id = job.get("worker_id")
                workers = self.api("GET", "/v1/service-workers").get("workers", [])
                owner = next(
                    (worker for worker in workers if worker.get("worker_id") == worker_id),
                    None,
                )
                if (
                    owner is None
                    or owner.get("status") not in {"idle", "running"}
                    or owner.get("job_id") == jobid
                ):
                    raise RuntimeError("SERVICE_PREVIOUS_EXECUTION_NOT_STOPPED")
                response = self.api(
                    "POST",
                    "/v1/service-jobs/" + jobid + "/retry",
                    {"previous_execution_stopped": True},
                    key="matrix-service-retry-" + jobid,
                )
                if response.get("status") != "queued":
                    raise RuntimeError("SERVICE_RETRY_NOT_QUEUED")
                self.record(
                    "service_retry",
                    job_id=jobid,
                    worker_id=worker_id,
                    previous_status=job["status"],
                    error_code=job["error"],
                    attempt=2,
                )
                retry_attempted = True
                continue
            if job["status"] in {"blocked", "failed", "uncertain", "cancelled"}:
                raise RuntimeError(
                    "SERVICE_" + job["status"].upper() + "_" + str(job.get("error", ""))
                )
            time.sleep(2)
        raise RuntimeError("SERVICE_TIMEOUT")

    def cleanup(self, item):
        self.wait_operation(
            self.api(
                "POST",
                "/v1/instances/" + item["instance_id"] + "/power/shutdown",
                key="shutdown-" + item["name"],
            )
        )
        self.wait_operation(
            self.api(
                "DELETE",
                "/v1/instances/" + item["instance_id"],
                key="delete-" + item["name"],
            )
        )
        assert not any(
            int(v.get("vmid", 0)) == item["vmid"] for v in self.proxmox.inventory()
        )
        state = self.api("GET", "/v1/instances/" + item["instance_id"])
        assert state["status"] == "deleted"
        if self.profile == "lab_dhcp":
            assert state["network_allocation"] is None
            self.record(
                "PASS",
                name=item["name"],
                product=item["product"],
                image=item["image"],
                plan=item["plan"],
                control=item["control"],
                vmid=item["vmid"],
                network_profile=self.profile,
            )
            return
        assert state["network_allocation"]["status"] == "released"
        # La misma dirección debe quedar ausente de NetBox, sin tocar registros ajenos.
        headers = {"Authorization": "Bearer " + self.env["IACP_NETBOX_TOKEN"]}
        req = Request(
            self.env["IACP_NETBOX_URL"].rstrip("/")
            + "/ipam/ip-addresses/?address="
            + item["address"]
            + "/24",
            headers=headers,
        )
        with urlopen(req, timeout=30) as f:
            assert json.load(f)["count"] == 0
        self.record(
            "PASS",
            name=item["name"],
            product=item["product"],
            image=item["image"],
            plan=item["plan"],
            control=item["control"],
            vmid=item["vmid"],
        )

    def run(self):
        planned = cases(self.args.category)
        self.record(
            "start",
            category=self.args.category,
            network_profile=self.profile,
            cycle=self.args.cycle,
            revision=self.env["IACP_IMAGE_TAG"],
            harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            cases=16,
            controls=2,
        )
        self.verify_network_profile()
        for offset in range(0, 18, 6):
            self.idle()
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                items = list(
                    pool.map(
                        lambda pair: self.create(*pair),
                        enumerate(planned[offset : offset + 6], start=offset + 1),
                    )
                )
            self.record(
                "injected",
                instances=[
                    {k: i[k] for k in ["name", "instance_id", "operation_id"]}
                    for i in items
                ],
            )
            peak = 0
            for _ in range(900):
                snapshot = self.api("GET", "/v1/observability")
                peak = max(peak, snapshot["active_operations"])
                states = [
                    self.api("GET", "/v1/operations/" + i["operation_id"])["status"]
                    for i in items
                ]
                if any(s in {"failed", "uncertain"} for s in states):
                    raise RuntimeError("PROVISION_FAILED_OR_UNCERTAIN")
                if all(s in {"completed", "succeeded"} for s in states):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("BATCH_TIMEOUT")
            assert peak == 6, "SIX_SIMULTANEOUS_NOT_OBSERVED"
            self.record("concurrency", simultaneous_peak=peak)
            for item in items:
                if item["product"] != "kvm":
                    self.wait_service(item["job_id"])
            self.parallel(self.verify, items)
            self.parallel(self.cleanup, items)
        self.idle()
        self.record("COMPLETE", result="PASS", cases=16, controls=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path("/etc/iacp/stacks/dev.env"))
    parser.add_argument(
        "--category", choices=["kvm", "services", "agents"], required=True
    )
    parser.add_argument("--cycle", type=int, choices=[1, 2], required=True)
    parser.add_argument(
        "--network-profile", choices=["lab_dhcp", "prod_static"], default="prod_static"
    )
    parser.add_argument("--run", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if not args.run.isdigit() or len(args.run) > 14:
        parser.error("run debe ser una marca numérica de hasta 14 dígitos")
    matrix = Matrix(args)
    try:
        matrix.run()
    except Exception as error:
        # No serializar errores arbitrarios: pueden contener credenciales HTTP.
        matrix.record(
            "BLOCKED",
            reason=str(error)
            if type(error) in {AssertionError, RuntimeError}
            else type(error).__name__,
        )
        sys.exit(1)
