#!/usr/bin/env python3
"""Run the real MNG v1 lifecycle matrix with bounded concurrency.

The runner intentionally keeps credentials in the process environment and
prints only redacted operation results. It is for controlled lab validation,
not for production provisioning.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from threading import Lock
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener


BASE_URL = os.getenv("IACP_E2E_BASE_URL", "https://dev-cyberfuel.com").rstrip("/")
API_TOKEN = os.environ["IACP_E2E_API_TOKEN"]
PROXMOX_ENDPOINT = os.getenv("IACP_E2E_PROXMOX_ENDPOINT", "").rstrip("/")
PROXMOX_TOKEN = os.getenv("IACP_E2E_PROXMOX_TOKEN", "")
POOL_ID = os.getenv("IACP_E2E_POOL_ID", "iacp-lab")
PRIVATE_KEY = Path(os.getenv("IACP_E2E_SSH_PRIVATE_KEY", "/home/despinoza/.ssh/iacp-lab-kvm"))
PUBLIC_KEY = os.getenv("IACP_E2E_SSH_PUBLIC_KEY", "").strip()
KEY_DIR = Path(os.getenv("IACP_E2E_KEY_DIR", "")) if os.getenv("IACP_E2E_KEY_DIR") else None
MAX_WORKERS = int(os.getenv("IACP_E2E_MAX_WORKERS", "2"))
OPERATION_TIMEOUT = int(os.getenv("IACP_E2E_OPERATION_TIMEOUT", "1800"))
NETWORK_TIMEOUT = int(os.getenv("IACP_E2E_NETWORK_TIMEOUT", "360"))
SSH_TIMEOUT = int(os.getenv("IACP_E2E_SSH_TIMEOUT", "360"))
SSH_ATTEMPT_TIMEOUT = int(os.getenv("IACP_E2E_SSH_ATTEMPT_TIMEOUT", "45"))
PING_TIMEOUT = int(os.getenv("IACP_E2E_PING_TIMEOUT", "8"))
RUN_ID = os.getenv("IACP_E2E_RUN_ID", time.strftime("%Y%m%d%H%M%S", time.gmtime()))
CHANNEL = os.getenv("IACP_E2E_CHANNEL", "cli")
NETWORK_PROFILE = os.getenv("IACP_E2E_NETWORK_PROFILE", "lab_dhcp")

IMAGES = ("ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10")
PLANS = ("kvm-1", "kvm-2", "kvm-3", "kvm-4")
_TEST_PASSWORDS: set[str] = set()
_TEST_PASSWORDS_LOCK = Lock()
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
            return True
        members = payload.get("members") or []
        if any(isinstance(member, dict) and str(member.get("vmid")) == str(vmid) for member in members):
            return True
        inventory = proxmox("/cluster/resources?type=vm")
        if isinstance(inventory, list):
            for item in inventory:
                if isinstance(item, dict) and str(item.get("vmid")) == str(vmid):
                    if item.get("pool") == POOL_ID:
                        return True
                    break
        time.sleep(3)
    return False


def vm_exists(vmid: int) -> bool:
    inventory = proxmox("/cluster/resources?type=vm")
    return bool(
        isinstance(inventory, list)
        and any(isinstance(item, dict) and int(item.get("vmid", -1)) == vmid for item in inventory)
    )


def cleanup(instance_id: str | None, label: str) -> str:
    if not instance_id:
        return "no-instance"
    try:
        current = instance(instance_id)
        if current.get("status") == "deleted":
            return "already-deleted"
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
        return "destroyed"
    except Exception as error:
        print(f"CLEANUP {label}: {redact(error)}", flush=True)
        return "cleanup-failed"


def run_case(case: dict) -> dict:
    label = case["label"]
    image = case["image"]
    plan = case["plan"]
    mode = case["mode"]
    name = f"e2e-{CHANNEL}-{RUN_ID}-{case['index']:02d}-{image.replace('-', '')}-{plan.replace('-', '')}-{mode}"
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
    ping_statuses: list[str] = []
    case_started = time.monotonic()
    stage_seconds: dict[str, float] = {}
    try:
        stage_started = time.monotonic()
        created = api("POST", "/v1/instances", body=body, idempotency_key=create_key)
        instance_id = created.get("instance_id") if isinstance(created, dict) else None
        create_operation = operation_id(created)
        replay = api("POST", "/v1/instances", body=body, idempotency_key=create_key)
        if operation_id(replay) != create_operation or replay.get("instance_id") != instance_id:
            raise E2EError("Idempotency-Key no repitió la misma creación")
        wait_operation(create_operation)
        current = instance(instance_id)
        vmid = int(current["vmid"])
        if vmid < 40001:
            raise E2EError(f"VMID fuera de rango: {vmid}")
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
            wait_operation(operation_id(started))
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
        ping_statuses.append(ping_status)
        stage_seconds["ssh_initial"] = round(time.monotonic() - stage_started, 3)
        stage_started = time.monotonic()
        reboot = api(
            "POST",
            f"/v1/instances/{quote(instance_id, safe='')}/power/reboot",
            idempotency_key=f"{label}-reboot",
        )
        wait_operation(operation_id(reboot))
        wait_power(instance_id, "running")
        stage_seconds["reboot"] = round(time.monotonic() - stage_started, 3)
        stage_started = time.monotonic()
        ip, _, ping_status = wait_ssh(instance_id, mode, password, private_key)
        ping_statuses.append(ping_status)
        stage_seconds["ssh_reboot"] = round(time.monotonic() - stage_started, 3)
        stage_started = time.monotonic()
        shutdown = api(
            "POST",
            f"/v1/instances/{quote(instance_id, safe='')}/power/shutdown",
            idempotency_key=f"{label}-shutdown",
        )
        wait_operation(operation_id(shutdown))
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
        wait_operation(operation_id(destroy))
        deleted = True
        final = instance(instance_id)
        if final.get("status") != "deleted":
            raise E2EError(f"destroy no dejó la instancia eliminada: {redact(final)}")
        if vm_exists(vmid):
            raise E2EError(f"VMID {vmid} sigue presente en Proxmox")
        stage_seconds["destroy"] = round(time.monotonic() - stage_started, 3)
        duration_seconds = round(time.monotonic() - case_started, 3)
        print(
            f"PASS {label} vmid={vmid} ip={ip} ssh={mode} explicit_start={started_explicitly} duration={duration_seconds:.1f}s ping={','.join(ping_statuses)}",
            flush=True,
        )
        return {
            "label": label,
            "status": "passed",
            "vmid": vmid,
            "ip": ip,
            "ping": ping_statuses,
            "duration_seconds": duration_seconds,
            "stages": stage_seconds,
        }
    except Exception as error:
        duration_seconds = round(time.monotonic() - case_started, 3)
        cleanup_status = cleanup(instance_id, label) if not deleted else "already-deleted"
        print(
            f"FAIL {label} vmid={vmid or '-'} cleanup={cleanup_status}: {redact(error)}",
            flush=True,
        )
        return {
            "label": label,
            "status": "failed",
            "vmid": vmid,
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
    if environments.get("default_environment") != "lab" or "lab" not in {
        item.get("environment") for item in environments.get("environments", [])
    }:
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
    observability = api("GET", "/v1/observability")
    capacity = observability.get("max_concurrent") if isinstance(observability, dict) else None
    if capacity is not None and int(capacity) < MAX_WORKERS:
        raise E2EError(f"capacidad observada insuficiente: max_concurrent={capacity}, workers solicitados={MAX_WORKERS}")
    print(f"CAPACITY PASS configured={capacity if capacity is not None else 'unknown'} requested={MAX_WORKERS}", flush=True)


def main() -> int:
    if not 1 <= MAX_WORKERS <= 2:
        raise SystemExit("IACP_E2E_MAX_WORKERS debe estar entre 1 y 2")
    if not PROXMOX_ENDPOINT or not PROXMOX_TOKEN:
        raise SystemExit(
            "IACP_E2E_PROXMOX_ENDPOINT y IACP_E2E_PROXMOX_TOKEN son obligatorios para validar limpieza física"
        )
    if NETWORK_PROFILE != "lab_dhcp":
        raise SystemExit("esta matriz debe ejecutarse con IACP_E2E_NETWORK_PROFILE=lab_dhcp")
    validate_catalog()
    cases = []
    index = 1
    for image in IMAGES:
        for plan in PLANS:
            for mode in ("password", "key"):
                cases.append(
                    {
                        "index": index,
                        "image": image,
                        "plan": plan,
                        "mode": mode,
                        "label": f"{RUN_ID}-{index:02d}-{image}-{plan}-{mode}",
                    }
                )
                index += 1
    results = []
    matrix_started = time.monotonic()
    total_waves = (len(cases) + MAX_WORKERS - 1) // MAX_WORKERS
    for wave_number, wave in enumerate(chunks(cases, MAX_WORKERS), 1):
        print(
            f"WAVE {wave_number}/{total_waves} start="
            + ",".join(f"{case['image']}/{case['plan']}/{case['mode']}" for case in wave),
            flush=True,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(run_case, case) for case in wave]
            results.extend(future.result() for future in futures)
        failed = [result for result in results[-len(wave) :] if result["status"] != "passed"]
        print(f"WAVE {wave_number}/{total_waves} done failed={len(failed)}", flush=True)
        if failed:
            print(
                f"ABORT after wave {wave_number}: se detiene para reparar sin crear más VMs.",
                flush=True,
            )
            break
    passed = sum(result["status"] == "passed" for result in results)
    failed = len(results) - passed
    print(f"SUMMARY total={len(results)} passed={passed} failed={failed}", flush=True)
    print_matrix_metrics(results, time.monotonic() - matrix_started)
    if failed:
        for result in results:
            if result["status"] != "passed":
                print(json.dumps(result, sort_keys=True), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MATRIX ABORTED: {redact(error)}", file=sys.stderr, flush=True)
        raise
