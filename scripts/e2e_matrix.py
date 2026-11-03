#!/usr/bin/env python3
"""Ejecuta la matriz real de lifecycle v1 de iac.cyberfuel.com sobre MNG.

Las credenciales permanecen en el entorno del proceso y la salida redacta
resultados operativos. La matriz valida el runtime de laboratorio publicado
bajo el entorno lógico ``lab``.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from threading import Event, Lock, Thread
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener


BASE_URL = os.getenv("IACP_E2E_BASE_URL", "https://iac.cyberfuel.com").rstrip("/")
API_TOKEN = os.getenv("IACP_E2E_API_TOKEN") or os.getenv("IACP_API_TOKEN", "")
PROXMOX_ENDPOINT = (
    os.getenv("IACP_E2E_MNG_PROXMOX_ENDPOINT")
    or os.getenv("IACP_PROXMOX_MNG_ENDPOINT")
    or os.getenv("IACP_E2E_PROXMOX_ENDPOINT", "")
).rstrip("/")
PROXMOX_TOKEN = (
    os.getenv("IACP_E2E_MNG_PROXMOX_TOKEN")
    or os.getenv("IACP_PROXMOX_MNG_API_TOKEN")
    or os.getenv("IACP_E2E_PROXMOX_TOKEN", "")
)
POOL_ID = os.getenv("IACP_E2E_POOL_ID", "iacp-lab")
PRIVATE_KEY = Path(os.getenv("IACP_E2E_SSH_PRIVATE_KEY", "/home/despinoza/.ssh/iacp-lab-kvm"))
PUBLIC_KEY = os.getenv("IACP_E2E_SSH_PUBLIC_KEY", "").strip()
KEY_DIR = Path(os.getenv("IACP_E2E_KEY_DIR", "")) if os.getenv("IACP_E2E_KEY_DIR") else None
MAX_WORKERS = int(os.getenv("IACP_E2E_MAX_WORKERS", "2"))
VMID_MIN = int(os.getenv("IACP_E2E_VMID_MIN", "30000"))
VMID_MAX = int(os.getenv("IACP_E2E_VMID_MAX", "39999"))
OPERATION_TIMEOUT = int(os.getenv("IACP_E2E_OPERATION_TIMEOUT", "1800"))
NETWORK_TIMEOUT = int(os.getenv("IACP_E2E_NETWORK_TIMEOUT", "360"))
SSH_TIMEOUT = int(os.getenv("IACP_E2E_SSH_TIMEOUT", "360"))
SSH_ATTEMPT_TIMEOUT = int(os.getenv("IACP_E2E_SSH_ATTEMPT_TIMEOUT", "45"))
PING_TIMEOUT = int(os.getenv("IACP_E2E_PING_TIMEOUT", "8"))
RUN_ID = os.getenv("IACP_E2E_RUN_ID", time.strftime("%Y%m%d%H%M%S", time.gmtime()))
CHANNEL = "iac-prod-mng"
NETWORK_PROFILE = os.getenv("IACP_E2E_NETWORK_PROFILE", "lab_dhcp")
EVIDENCE_FILE = Path(os.environ["IACP_E2E_EVIDENCE_FILE"]) if os.getenv("IACP_E2E_EVIDENCE_FILE") else None
RUN_PREFIX = f"e2e-{CHANNEL}-{RUN_ID}"

IMAGES = ("ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10")
PLANS = ("kvm-1", "kvm-2", "kvm-3", "kvm-4")
_TEST_PASSWORDS: set[str] = set()
_TEST_PASSWORDS_LOCK = Lock()
_RUN_INSTANCE_IDS: set[str] = set()
_RUN_INSTANCE_IDS_LOCK = Lock()
_LIVE_IPS: dict[str, str] = {}
_LIVE_IPS_LOCK = Lock()
_CONTENTION = Event()
_CONTENTION_DETAILS: list[str] = []
_CONTENTION_LOCK = Lock()
_PASSWORD_CLASSES = ("ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "!@#%+-_")
_PASSWORD_ALPHABET = "".join(_PASSWORD_CLASSES)
KEY_VARIANTS = ("ed25519-a", "rsa-3072", "ecdsa-p256", "ed25519-b")


class E2EError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: object = None):
        super().__init__(message)
        self.status = status
        self.body = body


def redact(value: object) -> str:
    text = str(value)
    with _TEST_PASSWORDS_LOCK:
        generated_passwords = tuple(_TEST_PASSWORDS)
    secrets_to_redact = [API_TOKEN, PROXMOX_TOKEN, PUBLIC_KEY, *generated_passwords]
    if KEY_DIR:
        for variant in KEY_VARIANTS:
            public_path = KEY_DIR / f"{variant}.pub"
            if public_path.exists():
                secrets_to_redact.append(public_path.read_text(encoding="utf-8").strip())
    for secret in secrets_to_redact:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text.replace("root_password", "<secret>")


def validate_execution_config() -> None:
    parsed = urlsplit(BASE_URL)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "iac.cyberfuel.com"
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise E2EError("IACP_E2E_BASE_URL debe ser exactamente https://iac.cyberfuel.com")
    if not API_TOKEN:
        raise E2EError("IACP_E2E_API_TOKEN es obligatorio para la API oficial de IAC")
    if not PROXMOX_ENDPOINT.startswith("https://") or not PROXMOX_TOKEN:
        raise E2EError("las credenciales de lectura Proxmox del clúster MNG son obligatorias")
    if not re.fullmatch(r"\d{14}", RUN_ID):
        raise E2EError("IACP_E2E_RUN_ID debe usar 14 dígitos UTC: AAAAMMDDHHMMSS")
    if MAX_WORKERS != 2:
        raise E2EError("la matriz física debe limitarse a dos aprovisionamientos simultáneos")
    if (VMID_MIN, VMID_MAX) != (30000, 39999):
        raise E2EError("la matriz de iac.cyberfuel.com solo puede usar VMID 30000–39999")
    if NETWORK_PROFILE != "lab_dhcp":
        raise E2EError("esta matriz debe ejecutarse con IACP_E2E_NETWORK_PROFILE=lab_dhcp")
    if EVIDENCE_FILE is None:
        raise E2EError("IACP_E2E_EVIDENCE_FILE debe conservar el reporte fuera del volumen de workdata")
    evidence_path = EVIDENCE_FILE.expanduser().resolve(strict=False)
    workdata_path = Path(os.getenv("IACP_WORKDATA_DIR", "/var/lib/iacp-controlplane")).expanduser().resolve(strict=False)
    try:
        evidence_path.relative_to(workdata_path)
    except ValueError:
        pass
    else:
        raise E2EError("la evidencia debe quedar fuera de IACP_WORKDATA_DIR")
    if evidence_path.exists():
        raise E2EError("el archivo de evidencia ya existe; usa un RUN_ID nuevo para no reemplazar registros")


def _record_contention(detail: str) -> None:
    _CONTENTION.set()
    with _CONTENTION_LOCK:
        if detail not in _CONTENTION_DETAILS:
            _CONTENTION_DETAILS.append(detail)


def _operation_instance_name(operation: dict) -> str:
    name = operation.get("instance_name")
    if name:
        return str(name)
    payload = operation.get("payload") or {}
    spec = payload.get("spec") or {}
    return str(spec.get("name") or "")


def _is_run_operation(operation: dict) -> bool:
    if _operation_instance_name(operation).startswith(RUN_PREFIX):
        return True
    instance_id = operation.get("instance_id")
    with _RUN_INSTANCE_IDS_LOCK:
        return bool(instance_id and str(instance_id) in _RUN_INSTANCE_IDS)


def _runtime_snapshot() -> dict:
    payload = api("GET", "/v1/observability")
    if not isinstance(payload, dict):
        raise E2EError("la observabilidad de IAC devolvió una respuesta inválida")
    return payload


def require_idle_runtime() -> dict:
    observed = _runtime_snapshot()
    workers = observed.get("workers") or []
    if int(observed.get("max_concurrent", 0)) != 6:
        raise E2EError("el runtime de iac.cyberfuel.com no anuncia concurrencia configurada de seis")
    if int(observed.get("active_operations", -1)) != 0:
        raise E2EError("hay operaciones activas en la cola compartida; no se inyectan pruebas")
    if int(observed.get("queue_depth", -1)) != 0:
        raise E2EError("la cola compartida no está vacía; no se inyectan pruebas")
    if int(observed.get("uncertain_operations", -1)) != 0:
        raise E2EError("hay operaciones uncertain; se requiere reconciliación antes de probar")
    if len(workers) != 6 or any(
        worker.get("liveness") != "healthy"
        or worker.get("status") != "idle"
        or worker.get("current_operation_id")
        for worker in workers
    ):
        raise E2EError("se requieren los seis workers saludables, idle y sin operación asignada")
    return observed


class RuntimeSampler:
    """Mide carga y detecta actividad ajena sin intervenir workers compartidos."""

    def __init__(self):
        self.stop_event = Event()
        self.metrics = {
            "samples": 0,
            "peak_active_operations": 0,
            "peak_queue_depth": 0,
            "peak_test_busy_workers": 0,
            "foreign_activity": [],
            "sampling_errors": [],
        }
        self.thread = Thread(target=self._run, name="iac-e2e-observability", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> dict:
        self.stop_event.set()
        self.thread.join(timeout=10)
        return self.metrics

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                observed = _runtime_snapshot()
                self.metrics["samples"] += 1
                self.metrics["peak_active_operations"] = max(
                    self.metrics["peak_active_operations"],
                    int(observed.get("active_operations", 0)),
                )
                self.metrics["peak_queue_depth"] = max(
                    self.metrics["peak_queue_depth"], int(observed.get("queue_depth", 0))
                )
                test_busy = 0
                for worker in observed.get("workers") or []:
                    current = worker.get("current_operation") or {}
                    if current:
                        name = _operation_instance_name(current)
                        if _is_run_operation(current):
                            test_busy += 1
                        else:
                            detail = f"worker={worker.get('worker_id')} instance={name or 'unknown'}"
                            if detail not in self.metrics["foreign_activity"]:
                                self.metrics["foreign_activity"].append(detail)
                            _record_contention(detail)
                self.metrics["peak_test_busy_workers"] = max(
                    self.metrics["peak_test_busy_workers"], test_busy
                )
                for operation in observed.get("recent_operations") or []:
                    status = operation.get("internal_status") or operation.get("status")
                    if status in {"pending", "queued", "planning", "planned", "applying", "running", "destroying", "powering"}:
                        name = _operation_instance_name(operation)
                        if not _is_run_operation(operation):
                            detail = f"operation={operation.get('operation_id')} instance={name or 'unknown'}"
                            if detail not in self.metrics["foreign_activity"]:
                                self.metrics["foreign_activity"].append(detail)
                            _record_contention(detail)
            except Exception as error:
                detail = f"observabilidad no disponible durante la matriz: {redact(error)}"
                self.metrics["sampling_errors"].append(detail)
                _record_contention(detail)
            self.stop_event.wait(2)


def _json_body(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw[:500].decode("utf-8", errors="replace")


def http_json(
    method: str,
    url: str,
    *,
    body: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> tuple[int, object]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with build_opener().open(request, timeout=timeout) as response:
            return response.status, _json_body(response.read())
    except HTTPError as error:
        payload = _json_body(error.read())
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise E2EError(
            f"HTTP {error.code} {method} {url}: {redact(detail)}",
            status=error.code,
            body=payload,
        ) from error
    except (URLError, TimeoutError) as error:
        raise E2EError(f"HTTP {method} {url}: {redact(error)}") from error


def api(
    method: str,
    path: str,
    *,
    body: object | None = None,
    idempotency_key: str | None = None,
) -> object:
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    _, payload = http_json(method, f"{BASE_URL}{path}", body=body, headers=headers)
    return payload


def proxmox(path: str) -> object:
    if not PROXMOX_ENDPOINT or not PROXMOX_TOKEN:
        return None
    _, payload = http_json(
        "GET",
        f"{PROXMOX_ENDPOINT}/api2/json{path}",
        headers={"Authorization": f"PVEAPIToken={PROXMOX_TOKEN}"},
        timeout=30,
    )
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def operation_id(payload: object) -> str:
    if not isinstance(payload, dict) or not payload.get("operation_id"):
        raise E2EError(f"respuesta sin operation_id: {redact(payload)}")
    return str(payload["operation_id"])


def operation_status(payload: object) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    return str(payload.get("status") or payload.get("normalized_status") or "unknown")


def wait_operation(operation: str) -> dict:
    deadline = time.monotonic() + OPERATION_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        payload = api("GET", f"/v1/operations/{quote(operation, safe='')}")
        if isinstance(payload, dict):
            last = payload
        status = operation_status(payload)
        if status in {"completed", "failed", "uncertain"}:
            if status != "completed":
                error = payload.get("error_code") or payload.get("error") if isinstance(payload, dict) else payload
                raise E2EError(f"operación {operation} terminó en {status}: {redact(error)}", body=payload)
            return payload
        time.sleep(3)
    raise E2EError(f"timeout esperando operación {operation}: {redact(last)}")


def instance(instance_id: str) -> dict:
    payload = api("GET", f"/v1/instances/{quote(instance_id, safe='')}")
    if not isinstance(payload, dict):
        raise E2EError(f"instancia inválida: {redact(payload)}")
    return payload


def power(instance_id: str) -> dict:
    payload = api("GET", f"/v1/instances/{quote(instance_id, safe='')}/power")
    if not isinstance(payload, dict):
        raise E2EError(f"estado de energía inválido: {redact(payload)}")
    return payload


def wait_power(instance_id: str, expected: str) -> dict:
    deadline = time.monotonic() + 180
    last = None
    while time.monotonic() < deadline:
        try:
            last = power(instance_id)
            if last.get("power_state") == expected:
                return last
        except E2EError as error:
            last = {"error": redact(error)}
        time.sleep(3)
    raise E2EError(f"la instancia no llegó a {expected}: {redact(last)}")


def wait_network(instance_id: str) -> str:
    deadline = time.monotonic() + NETWORK_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        payload = api("GET", f"/v1/instances/{quote(instance_id, safe='')}/network")
        if isinstance(payload, dict):
            last = payload
            for value in payload.get("observed_ipv4", []):
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if address.version == 4 and not address.is_loopback and not address.is_link_local:
                    return str(address)
        time.sleep(5)
    raise E2EError(f"no se observó IPv4 por Guest Agent: {redact(last)}")


def ssh_command(
    ip: str,
    mode: str,
    password: str | None = None,
    private_key: Path | None = None,
) -> str:
    private_key = private_key or PRIVATE_KEY
    if mode == "key" and (not private_key.exists() or private_key.stat().st_mode & 0o077):
        raise E2EError(f"llave privada ausente o insegura: {private_key}")
    base = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ConnectTimeout={min(20, SSH_ATTEMPT_TIMEOUT)}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        "LogLevel=ERROR",
        "root@" + ip,
        "id -u",
    ]
    environment = os.environ.copy()
    askpass_dir = None
    if mode == "key":
        base[1:1] = ["-i", str(private_key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
    else:
        if not password:
            raise E2EError("falta contraseña para la prueba SSH")
        askpass_dir = tempfile.TemporaryDirectory(prefix="iacp-ssh-")
        askpass = Path(askpass_dir.name) / "askpass"
        askpass.write_text("#!/bin/sh\nprintf '%s\\n' \"$IACP_E2E_SSH_PASSWORD\"\n", encoding="utf-8")
        askpass.chmod(0o700)
        base[1:1] = [
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "BatchMode=no",
        ]
        environment.update(
            {
                "DISPLAY": "iacp-e2e",
                "SSH_ASKPASS": str(askpass),
                "SSH_ASKPASS_REQUIRE": "force",
                "IACP_E2E_SSH_PASSWORD": password,
            }
        )
    try:
        completed = subprocess.run(
            ["setsid", *base],
            check=False,
            capture_output=True,
            text=True,
            timeout=min(SSH_TIMEOUT, SSH_ATTEMPT_TIMEOUT),
            stdin=subprocess.DEVNULL,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise E2EError(f"SSH {mode} {ip}: {redact(error)}") from error
    finally:
        if askpass_dir is not None:
            askpass_dir.cleanup()
    if completed.returncode != 0 or completed.stdout.strip() != "0":
        raise E2EError(
            f"SSH {mode} {ip} falló rc={completed.returncode}: "
            f"{redact(completed.stderr.strip() or completed.stdout.strip())}"
        )
    return completed.stdout.strip()


def ping_host(ip: str) -> str:
    """Record ICMP reachability without making it a hard prerequisite for SSH."""

    try:
        completed = subprocess.run(
            ["ping", "-c", "2", "-W", "2", ip],
            check=False,
            capture_output=True,
            text=True,
            timeout=PING_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return "success" if completed.returncode == 0 else "blocked_or_timeout"


def wait_ssh(
    instance_id: str,
    mode: str,
    password: str | None = None,
    private_key: Path | None = None,
) -> tuple[str, str, str]:
    """Re-observe the VM address while cloud-init and DHCP converge.

    A recycled DHCP lease can briefly leave the Guest Agent reporting an old
    address after a previous test VM was destroyed. Re-reading the address on
    every attempt prevents the SSH check from pinning itself to that stale
    lease and also makes the identity being tested explicit.
    """

    deadline = time.monotonic() + SSH_TIMEOUT
    last: Exception | None = None
    last_ip = "pending"
    while time.monotonic() < deadline:
        try:
            network = api("GET", f"/v1/instances/{quote(instance_id, safe='')}/network")
        except E2EError as error:
            last = error
            time.sleep(5)
            continue
        addresses = network.get("observed_ipv4", []) if isinstance(network, dict) else []
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.version != 4 or parsed.is_loopback or parsed.is_link_local:
                continue
            last_ip = str(address)
            ping_status = ping_host(last_ip)
            try:
                return last_ip, ssh_command(last_ip, mode, password, private_key), ping_status
            except E2EError as error:
                last = error
        time.sleep(5)
    raise E2EError(f"SSH {mode} {last_ip} no estuvo disponible: {redact(last)}")


def pool_contains(vmid: int) -> bool:
    """Wait for Proxmox's pool and inventory views to converge after clone."""

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        payload = proxmox(f"/pools/{quote(POOL_ID, safe='')}")
        if not isinstance(payload, dict):
            raise E2EError(f"no se pudo verificar el pool {POOL_ID} en Proxmox")
        members = payload.get("members") or []
        if any(isinstance(member, dict) and str(member.get("vmid")) == str(vmid) for member in members):
            return True
        inventory = proxmox("/cluster/resources?type=vm")
        if not isinstance(inventory, list):
            raise E2EError("no se pudo verificar el inventario del clúster MNG")
        for item in inventory:
            if isinstance(item, dict) and str(item.get("vmid")) == str(vmid):
                if item.get("pool") == POOL_ID:
                    return True
                break
        time.sleep(3)
    return False


def vm_exists(vmid: int) -> bool:
    inventory = proxmox("/cluster/resources?type=vm")
    if not isinstance(inventory, list):
        raise E2EError("no se pudo verificar el inventario del clúster MNG")
    return any(isinstance(item, dict) and int(item.get("vmid", -1)) == vmid for item in inventory)


def claim_live_ip(instance_id: str, ip: str) -> None:
    with _LIVE_IPS_LOCK:
        existing = _LIVE_IPS.get(ip)
        if existing and existing != instance_id:
            raise E2EError(f"IP duplicada entre KVM de prueba simultáneas: {ip}")
        for current_ip, owner in list(_LIVE_IPS.items()):
            if owner == instance_id and current_ip != ip:
                del _LIVE_IPS[current_ip]
        _LIVE_IPS[ip] = instance_id


def release_live_ip(instance_id: str | None) -> None:
    if not instance_id:
        return
    with _LIVE_IPS_LOCK:
        for ip, owner in list(_LIVE_IPS.items()):
            if owner == instance_id:
                del _LIVE_IPS[ip]


def instance_by_name(name: str) -> dict | None:
    payload = api("GET", f"/v1/instances?{urlencode({'name': name, 'include_deleted': 'true', 'limit': 10})}")
    rows = payload.get("instances", []) if isinstance(payload, dict) else []
    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
    if len(matches) > 1:
        raise E2EError(f"nombre de prueba no único en IAC: {name}")
    return matches[0] if matches else None


def cleanup(instance_id: str | None, label: str) -> str:
    if not instance_id:
        return "no-instance"
    try:
        current = instance(instance_id)
        if current.get("status") == "deleted":
            vmid = current.get("vmid")
            if vmid is not None and vm_exists(int(vmid)):
                return "cleanup-failed: VM física presente tras estado deleted"
            release_live_ip(instance_id)
            return "already-deleted"
        latest = current.get("latest_operation") or {}
        if latest.get("status") in {"queued", "running"}:
            wait_operation(str(latest.get("operation_id") or latest.get("job_id")))
            current = instance(instance_id)
            latest = current.get("latest_operation") or {}
        if current.get("status") == "uncertain" or latest.get("status") == "uncertain":
            return "cleanup-blocked: requiere reconciliación controlada"
        vmid = current.get("vmid")
        if vmid is not None:
            try:
                if power(instance_id).get("power_state") == "running":
                    start = api(
                        "POST",
                        f"/v1/instances/{quote(instance_id, safe='')}/power/shutdown",
                        idempotency_key=f"{label}-cleanup-shutdown",
                    )
                    wait_operation(operation_id(start))
                    wait_power(instance_id, "stopped")
            except Exception as error:
                print(f"CLEANUP {label} shutdown: {redact(error)}", flush=True)
        destroy = api(
            "DELETE",
            f"/v1/instances/{quote(instance_id, safe='')}",
            idempotency_key=f"{label}-cleanup-destroy",
        )
        wait_operation(operation_id(destroy))
        final = instance(instance_id)
        if final.get("status") != "deleted":
            raise E2EError(f"cleanup no dejó la instancia eliminada: {redact(final)}")
        if vmid is not None and vm_exists(int(vmid)):
            raise E2EError(f"cleanup dejó VMID {vmid} presente en Proxmox")
        release_live_ip(instance_id)
        return "destroyed"
    except Exception as error:
        print(f"CLEANUP {label}: {redact(error)}", flush=True)
        return "cleanup-failed"


def run_case(case: dict) -> dict:
    label = case["label"]
    image = case["image"]
    plan = case["plan"]
    mode = case["mode"]
    name = f"{RUN_PREFIX}-{case['index']:02d}-{image.replace('-', '')}-{plan}"
    if len(name) > 63:
        raise E2EError(f"nombre de prueba excede 63 caracteres: {len(name)}")
    password_chars = [secrets.choice(characters) for characters in _PASSWORD_CLASSES]
    password_chars.extend(secrets.choice(_PASSWORD_ALPHABET) for _ in range(24))
    secrets.SystemRandom().shuffle(password_chars)
    password = "".join(password_chars)
    with _TEST_PASSWORDS_LOCK:
        _TEST_PASSWORDS.add(password)
    public_key = ""
    private_key = None
    if mode == "key":
        variant = KEY_VARIANTS[(case["index"] - 1) % len(KEY_VARIANTS)]
        if KEY_DIR:
            private_key = KEY_DIR / variant
            public_path = KEY_DIR / f"{variant}.pub"
            if public_path.exists():
                public_key = public_path.read_text(encoding="utf-8").strip()
        if not public_key:
            public_key = PUBLIC_KEY
        if not public_key:
            raise E2EError("falta la llave pública SSH para el caso de llave")
    body = {
        "name": name,
        "environment": "lab",
        "plan_id": plan,
        "image_id": image,
        "network_profile": NETWORK_PROFILE,
        "tags": ["e2e", f"cl-e2e-{case['index']:02d}", f"kvm-{case['index']:03d}"],
        "access": {"root_password": password, "ssh_public_keys": [public_key] if mode == "key" else []},
        "start_on_create": case["index"] % 2 == 0,
    }
    create_key = f"{label}-create"
    instance_id = None
    vmid = None
    deleted = False
    started_explicitly = False
    create_operation = None
    operation_ids: list[str] = []
    ping_statuses: list[str] = []
    current_ip = None
    case_started = time.monotonic()
    stage_seconds: dict[str, float] = {}
    try:
        stage_started = time.monotonic()
        created = api("POST", "/v1/instances", body=body, idempotency_key=create_key)
        instance_id = created.get("instance_id") if isinstance(created, dict) else None
        if instance_id:
            with _RUN_INSTANCE_IDS_LOCK:
                _RUN_INSTANCE_IDS.add(str(instance_id))
        create_operation = operation_id(created)
        operation_ids.append(create_operation)
        replay = api("POST", "/v1/instances", body=body, idempotency_key=create_key)
        if operation_id(replay) != create_operation or replay.get("instance_id") != instance_id:
            raise E2EError("Idempotency-Key no repitió la misma creación")
        wait_operation(create_operation)
        current = instance(instance_id)
        vmid = int(current["vmid"])
        if not VMID_MIN <= vmid <= VMID_MAX:
            raise E2EError(f"VMID fuera de rango: {vmid}")
        if current.get("environment") != "lab":
            raise E2EError(f"entorno lógico inesperado: {current.get('environment')}")
        if current.get("cluster") != "mng":
            raise E2EError(f"clúster inesperado para lab: {current.get('cluster')}")
        if current.get("image_id") != image or current.get("plan_id") != plan:
            raise E2EError(f"catálogo divergente: {redact(current)}")
        if (current.get("spec") or {}).get("network_profile") != NETWORK_PROFILE:
            raise E2EError(f"perfil de red divergente: {redact(current)}")
        if (current.get("spec") or {}).get("tags") != body["tags"]:
            raise E2EError("los tags enviados no se conservaron en la instancia")
        if not pool_contains(vmid):
            raise E2EError(f"VMID {vmid} no pertenece a {POOL_ID}")
        stage_seconds["provision"] = round(time.monotonic() - stage_started, 3)
        stage_started = time.monotonic()
        if not body["start_on_create"]:
            started = api(
                "POST",
                f"/v1/instances/{quote(instance_id, safe='')}/power/start",
                idempotency_key=f"{label}-start",
            )
            started_operation = operation_id(started)
            operation_ids.append(started_operation)
            wait_operation(started_operation)
            started_explicitly = True
        wait_power(instance_id, "running")
        stage_seconds["start"] = round(time.monotonic() - stage_started, 3)
        # Refresh the public state and telemetry before the first SSH check.
        instance(instance_id)
        api("GET", f"/v1/instances/{quote(instance_id, safe='')}/network")
        api("GET", f"/v1/instances/{quote(instance_id, safe='')}/power")
        api("GET", f"/v1/events?operation_id={quote(create_operation, safe='')}&sort=asc&limit=200")
        api("GET", "/v1/workers")
        api("GET", "/v1/observability")
        stage_started = time.monotonic()
        ip, _, ping_status = wait_ssh(instance_id, mode, password, private_key)
        claim_live_ip(instance_id, ip)
        current_ip = ip
        ping_statuses.append(ping_status)
        stage_seconds["ssh_initial"] = round(time.monotonic() - stage_started, 3)
        stage_started = time.monotonic()
        reboot = api(
            "POST",
            f"/v1/instances/{quote(instance_id, safe='')}/power/reboot",
            idempotency_key=f"{label}-reboot",
        )
        reboot_operation = operation_id(reboot)
        operation_ids.append(reboot_operation)
        wait_operation(reboot_operation)
        wait_power(instance_id, "running")
        stage_seconds["reboot"] = round(time.monotonic() - stage_started, 3)
        stage_started = time.monotonic()
        ip, _, ping_status = wait_ssh(instance_id, mode, password, private_key)
        claim_live_ip(instance_id, ip)
        current_ip = ip
        ping_statuses.append(ping_status)
        stage_seconds["ssh_reboot"] = round(time.monotonic() - stage_started, 3)
        stage_started = time.monotonic()
        shutdown = api(
            "POST",
            f"/v1/instances/{quote(instance_id, safe='')}/power/shutdown",
            idempotency_key=f"{label}-shutdown",
        )
        shutdown_operation = operation_id(shutdown)
        operation_ids.append(shutdown_operation)
        wait_operation(shutdown_operation)
        wait_power(instance_id, "stopped")
        stage_seconds["shutdown"] = round(time.monotonic() - stage_started, 3)
        instance(instance_id)
        api("GET", f"/v1/instances/{quote(instance_id, safe='')}/network")
        api("GET", f"/v1/instances/{quote(instance_id, safe='')}/power")
        api("GET", "/v1/workers")
        api("GET", "/v1/observability")
        stage_started = time.monotonic()
        destroy = api(
            "DELETE",
            f"/v1/instances/{quote(instance_id, safe='')}",
            idempotency_key=f"{label}-destroy",
        )
        destroy_operation = operation_id(destroy)
        operation_ids.append(destroy_operation)
        wait_operation(destroy_operation)
        final = instance(instance_id)
        if final.get("status") != "deleted":
            raise E2EError(f"destroy no dejó la instancia eliminada: {redact(final)}")
        if vm_exists(vmid):
            raise E2EError(f"VMID {vmid} sigue presente en Proxmox")
        deleted = True
        release_live_ip(instance_id)
        stage_seconds["destroy"] = round(time.monotonic() - stage_started, 3)
        duration_seconds = round(time.monotonic() - case_started, 3)
        print(
            f"PASS {label} vmid={vmid} ip={ip} ssh={mode} explicit_start={started_explicitly} duration={duration_seconds:.1f}s ping={','.join(ping_statuses)}",
            flush=True,
        )
        return {
            "label": label,
            "name": name,
            "instance_id": instance_id,
            "create_operation_id": create_operation,
            "operation_ids": operation_ids,
            "status": "passed",
            "vmid": vmid,
            "ip": ip,
            "ping": ping_statuses,
            "duration_seconds": duration_seconds,
            "stages": stage_seconds,
        }
    except Exception as error:
        if not instance_id:
            try:
                replay = api("POST", "/v1/instances", body=body, idempotency_key=create_key)
                instance_id = replay.get("instance_id") if isinstance(replay, dict) else None
                if instance_id:
                    with _RUN_INSTANCE_IDS_LOCK:
                        _RUN_INSTANCE_IDS.add(str(instance_id))
            except Exception:
                try:
                    found = instance_by_name(name)
                    instance_id = found.get("instance_id") if found else None
                    if instance_id:
                        with _RUN_INSTANCE_IDS_LOCK:
                            _RUN_INSTANCE_IDS.add(str(instance_id))
                except Exception:
                    instance_id = None
        duration_seconds = round(time.monotonic() - case_started, 3)
        cleanup_status = cleanup(instance_id, label) if not deleted else "already-deleted"
        print(
            f"FAIL {label} vmid={vmid or '-'} cleanup={cleanup_status}: {redact(error)}",
            flush=True,
        )
        return {
            "label": label,
            "name": name,
            "instance_id": instance_id,
            "create_operation_id": create_operation,
            "operation_ids": operation_ids,
            "status": "failed",
            "vmid": vmid,
            "ip": current_ip,
            "error": redact(error),
            "cleanup": cleanup_status,
            "duration_seconds": duration_seconds,
            "stages": stage_seconds,
        }


def chunks(values: list[dict], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    remainder = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * remainder, 3)


def print_matrix_metrics(results: list[dict], wall_seconds: float) -> None:
    durations = [
        float(result["duration_seconds"])
        for result in results
        if result.get("status") == "passed" and result.get("duration_seconds") is not None
    ]
    stage_values: dict[str, list[float]] = {}
    for result in results:
        if result.get("status") != "passed":
            continue
        for stage, duration in (result.get("stages") or {}).items():
            stage_values.setdefault(stage, []).append(float(duration))
    throughput = len(results) / wall_seconds * 60 if wall_seconds > 0 else 0.0
    print(
        "METRICS "
        f"wall_minutes={wall_seconds / 60:.2f} "
        f"case_p50_seconds={percentile(durations, 0.50)} "
        f"case_p95_seconds={percentile(durations, 0.95)} "
        f"throughput_cases_per_minute={throughput:.2f}",
        flush=True,
    )
    for stage in sorted(stage_values):
        print(
            f"METRIC_STAGE {stage} p50_seconds={percentile(stage_values[stage], 0.50)} "
            f"p95_seconds={percentile(stage_values[stage], 0.95)}",
            flush=True,
        )


def validate_catalog() -> None:
    environments = api("GET", "/v1/environments")
    configured_environments = [item.get("environment") for item in environments.get("environments", [])]
    if environments.get("default_environment") != "lab" or configured_environments != ["lab"]:
        raise E2EError(f"entorno de laboratorio no disponible: {environments}")
    plans = api("GET", "/v1/plans")
    images = api("GET", "/v1/images")
    actual_plans = [item.get("plan_id") for item in plans.get("plans", [])]
    actual_images = [item.get("image_id") for item in images.get("images", [])]
    if actual_plans != list(PLANS) or actual_images != list(IMAGES):
        raise E2EError(f"catálogo inesperado planes={actual_plans} images={actual_images}")
    print(f"CATALOG PASS plans={','.join(actual_plans)} images={','.join(actual_images)}", flush=True)
    profiles = api("GET", "/v1/network-profiles")
    available_profiles = {item.get("profile_id"): item for item in profiles.get("network_profiles", [])}
    selected_profile = available_profiles.get(NETWORK_PROFILE)
    if not selected_profile or not selected_profile.get("enabled") or selected_profile.get("network_mode") != "dhcp":
        raise E2EError(f"perfil DHCP no disponible: {NETWORK_PROFILE}")
    print(f"NETWORK PROFILE PASS profile={NETWORK_PROFILE}", flush=True)
    inventory = proxmox("/cluster/resources?type=vm")
    pool = proxmox(f"/pools/{quote(POOL_ID, safe='')}")
    if not isinstance(inventory, list) or not isinstance(pool, dict):
        raise E2EError("no se pudo consultar el inventario y pool del clúster MNG")
    print(f"MNG PASS pool={POOL_ID} inventory_vms={len(inventory)}", flush=True)
    observability = api("GET", "/v1/observability")
    capacity = observability.get("max_concurrent") if isinstance(observability, dict) else None
    if capacity is None or int(capacity) != 6:
        raise E2EError(f"capacidad observada insuficiente: max_concurrent={capacity}, workers solicitados={MAX_WORKERS}")
    print(f"CAPACITY PASS configured={capacity} requested={MAX_WORKERS}", flush=True)


def write_evidence(results: list[dict], metrics: dict, outcome: str) -> None:
    if EVIDENCE_FILE is None:
        return
    report = {
        "run_id": RUN_ID,
        "base_url": BASE_URL,
        "runtime_stage": "production",
        "environment": "lab",
        "cluster": "MNG",
        "vmid_range": [VMID_MIN, VMID_MAX],
        "max_simultaneous_provisions": MAX_WORKERS,
        "images": list(IMAGES),
        "plans": list(PLANS),
        "outcome": outcome,
        "metrics": metrics,
        "contention_details": list(_CONTENTION_DETAILS),
        "results": results,
    }
    EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = EVIDENCE_FILE.with_suffix(EVIDENCE_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, EVIDENCE_FILE)
    print(f"EVIDENCE {EVIDENCE_FILE}", flush=True)


def main() -> int:
    validate_execution_config()
    if not 1 <= MAX_WORKERS <= 2:
        raise SystemExit("IACP_E2E_MAX_WORKERS debe estar entre 1 y 2")
    if not PROXMOX_ENDPOINT or not PROXMOX_TOKEN:
        raise SystemExit(
            "las credenciales Proxmox de solo lectura para MNG son obligatorias para validar limpieza física"
        )
    validate_catalog()
    baseline = require_idle_runtime()
    print(
        "BASELINE PASS "
        f"workers={len(baseline['workers'])} max_concurrent={baseline['max_concurrent']} "
        f"active={baseline['active_operations']} queue={baseline['queue_depth']} "
        f"uncertain={baseline['uncertain_operations']}",
        flush=True,
    )
    cases = []
    index = 1
    for image_index, image in enumerate(IMAGES):
        for plan_index, plan in enumerate(PLANS):
            mode = "password" if (image_index + plan_index) % 2 == 0 else "key"
            cases.append(
                {
                    "index": index,
                    "image": image,
                    "plan": plan,
                    "mode": mode,
                    "label": f"{RUN_ID}-{index:02d}-{image}-{plan}",
                }
            )
            index += 1
    results = []
    matrix_started = time.monotonic()
    total_waves = (len(cases) + MAX_WORKERS - 1) // MAX_WORKERS
    sampler = RuntimeSampler()
    sampler.start()
    try:
        for wave_number, wave in enumerate(chunks(cases, MAX_WORKERS), 1):
            if wave_number > 1:
                require_idle_runtime()
            print(
                f"WAVE {wave_number}/{total_waves} start="
                + ",".join(f"{case['image']}/{case['plan']}/{case['mode']}" for case in wave),
                flush=True,
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(run_case, case) for case in wave]
                wave_results = [future.result() for future in futures]
                results.extend(wave_results)
            failed = [result for result in wave_results if result["status"] != "passed"]
            print(f"WAVE {wave_number}/{total_waves} done failed={len(failed)}", flush=True)
            if failed or _CONTENTION.is_set():
                reason = "se detectó actividad externa o pérdida de observabilidad" if _CONTENTION.is_set() else "falló un caso"
                print(f"ABORT after wave {wave_number}: {reason}; no se inyectan más VMs.", flush=True)
                break
    finally:
        telemetry = sampler.stop()
    passed = sum(result["status"] == "passed" for result in results)
    failed = len(results) - passed
    print(f"SUMMARY total={len(results)} passed={passed} failed={failed}", flush=True)
    print_matrix_metrics(results, time.monotonic() - matrix_started)
    if passed == len(cases) and not _CONTENTION.is_set() and telemetry["peak_test_busy_workers"] >= 2:
        try:
            require_idle_runtime()
        except Exception as error:
            _record_contention(f"runtime no quedó idle al terminar: {redact(error)}")
    outcome = "PASS" if passed == len(cases) and not failed and not _CONTENTION.is_set() and telemetry["peak_test_busy_workers"] >= 2 else "BLOCKED" if _CONTENTION.is_set() else "FAIL"
    telemetry["contention_detected"] = _CONTENTION.is_set()
    write_evidence(results, telemetry, outcome)
    print(
        f"CONCURRENCY observed_peak_workers={telemetry['peak_test_busy_workers']} "
        f"configured_workers={MAX_WORKERS} samples={telemetry['samples']}",
        flush=True,
    )
    if outcome != "PASS":
        for result in results:
            if result["status"] != "passed":
                print(json.dumps(result, sort_keys=True), flush=True)
        if passed == len(cases) and telemetry["peak_test_busy_workers"] < 2:
            print("FAIL: no se observó evidencia simultánea de dos workers de prueba.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MATRIX ABORTED: {redact(error)}", file=sys.stderr, flush=True)
        raise
