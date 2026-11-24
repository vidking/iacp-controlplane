"""Ejecutor Ansible. Los bindings SSH y releases son entradas del operador, no del consumidor."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from ipaddress import ip_address
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from api.service_catalog import load_service_release, service_release_digest
from api.service_secrets import decrypt
from worker.service_recipes import render, RecipeError
from worker.service_diagnostics import (
    ServiceExecutionFailed,
    ServiceExecutionUncertain,
    safe_execution_error,
)
from worker.service_locks import current_service_job_lock_fd


NEXTCLOUD_READY_MAX_SECONDS = 20 * 60
NEXTCLOUD_READY_POLL_SECONDS = 5
NEXTCLOUD_READY_ATTEMPTS = NEXTCLOUD_READY_MAX_SECONDS // NEXTCLOUD_READY_POLL_SECONDS

NEXTCLOUD_READY_SCRIPT = (
    "set -eu\n"
    "attempt=0\n"
    f"while [ \"$attempt\" -lt {NEXTCLOUD_READY_ATTEMPTS} ]; do\n"
    "  if php occ status --output=json 2>/dev/null | grep -Eq '\"installed\"[[:space:]]*:[[:space:]]*true[[:space:]]*([,}])'; then\n"
    "    printf 'IACP_NEXTCLOUD_INSTALLED\\n'\n"
    "    exit 0\n"
    "  fi\n"
    "  attempt=$((attempt + 1))\n"
    f"  sleep {NEXTCLOUD_READY_POLL_SECONDS}\n"
    "done\n"
    "exit 1\n"
)

SERVICE_EGRESS_PREFLIGHT_SCRIPT = r'''import errno
import http.client
import random
import socket
import ssl
import subprocess
import time

targets = [
    ("archive.ubuntu.com", "/ubuntu/dists/noble/InRelease", False),
    ("security.ubuntu.com", "/ubuntu/dists/noble-security/InRelease", False),
    ("registry-1.docker.io", "/v2/", True),
]
DNS_TIMEOUT_SECONDS = 3
MAX_ATTEMPTS = 3
context = ssl.create_default_context()

def resolve_ipv4(host):
    try:
        result = subprocess.run(
            ["getent", "ahostsv4", host],
            capture_output=True,
            text=True,
            timeout=DNS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "DNS_TIMEOUT"
    except OSError:
        return None, "DNS"
    if result.returncode != 0:
        return None, "DNS"
    for line in result.stdout.splitlines():
        address = line.split(maxsplit=1)[0] if line.split() else ""
        try:
            socket.inet_pton(socket.AF_INET, address)
        except OSError:
            continue
        return address, ""
    return None, "DNS"

def https_probe(host, path, address, registry):
    try:
        raw = socket.create_connection((address, 443), timeout=5)
        with context.wrap_socket(raw, server_hostname=host) as connection:
            connection.settimeout(5)
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Range: bytes=0-0\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            connection.sendall(request)
            response = http.client.HTTPResponse(connection)
            response.begin()
            challenge = response.getheader("WWW-Authenticate", "")
            if 200 <= response.status < 300:
                return ""
            if (
                registry
                and response.status == 401
                and challenge.lower().startswith("bearer ")
            ):
                return ""
            return "HTTP"
    except ssl.SSLError:
        return "TLS"
    except (TimeoutError, socket.timeout):
        return "TIMEOUT"
    except OSError as error:
        if error.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
            return "ROUTE"
        if isinstance(error, ConnectionRefusedError):
            return "TCP"
        return "HTTPS"

last_error = {}
pending = {host: (path, registry) for host, path, registry in targets}
for attempt in range(MAX_ATTEMPTS):
    for host, (path, registry) in list(pending.items()):
        address, error = resolve_ipv4(host)
        if not error:
            error = https_probe(host, path, address, registry)
        if error:
            last_error[host] = error
        else:
            del pending[host]
    if pending and attempt + 1 < MAX_ATTEMPTS:
        time.sleep((2**attempt) + random.uniform(0, 0.5))

if pending:
    summary = ",".join(
        f"{host}:{last_error.get(host, 'TIMEOUT')}" for host in pending
    )
    raise SystemExit("IACP_SERVICE_EGRESS_PREFLIGHT_FAILED:" + summary)

print("IACP_SERVICE_EGRESS_PREFLIGHT_OK")
'''


def service_egress_preflight_task() -> dict:
    """Check DNS/TLS egress with bounded resolver and process timeouts."""

    return {
        "name": "Validar DNS y egress HTTPS antes de instalar dependencias",
        "ansible.builtin.command": {
            "argv": [
                "timeout",
                "--signal=TERM",
                "--kill-after=5s",
                "90s",
                "python3",
                "-c",
                SERVICE_EGRESS_PREFLIGHT_SCRIPT,
            ]
        },
        "changed_when": False,
    }

N8N_OWNER_SETUP_SCRIPT = """import {readFileSync} from 'node:fs';
const account = JSON.parse(Buffer.from(readFileSync(0, 'utf8').trim(), 'base64').toString('utf8'));
const base = 'http://127.0.0.1:5678/rest';
async function post(path, value) {
  const response = await fetch(base + path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(value),
  });
  let body = {};
  try { body = await response.json(); } catch {}
  return {status: response.status, body: body.data ?? body};
}
const setup = await post('/owner/setup', {
  email: account.email,
  firstName: account.firstName,
  lastName: account.lastName,
  password: account.password,
});
const created = setup.status === 200 && setup.body.email === account.email;
// Confirmar el acceso del propietario antes de publicar el proxy. En una
// reanudación, el mismo login verifica el alta anterior.
const login = await post('/login', {
  emailOrLdapLoginId: account.email,
  password: account.password,
});
if (login.status === 200 && login.body.email === account.email) {
  console.log(created ? 'IACP_N8N_OWNER_CREATED' : 'IACP_N8N_OWNER_VERIFIED');
  process.exit(0);
}
console.error('IACP_N8N_OWNER_SETUP_FAILED');
process.exit(1);
"""


def ubuntu_https_sources_task() -> dict:
    """Use authenticated HTTPS for the standard Ubuntu Noble repositories."""

    return {
        "name": "Usar HTTPS en los repositorios estándar de Ubuntu",
        "ansible.builtin.replace": {
            "path": "/etc/apt/sources.list.d/ubuntu.sources",
            "regexp": r"(?m)^URIs: http://(archive|security)\.ubuntu\.com/ubuntu$",
            "replace": r"URIs: https://\1.ubuntu.com/ubuntu",
        },
    }


def nextcloud_status_argv(base: list[str]) -> list[str]:
    """Espera hasta 20 minutos a que OCC confirme la instalación de Nextcloud."""

    return base + [
        "exec",
        "-T",
        "--user",
        "www-data",
        "app",
        "sh",
        "-ec",
        NEXTCLOUD_READY_SCRIPT,
    ]


def n8n_owner_setup_task(base: list[str], configuration: dict, secrets: dict) -> dict:
    """Crear y verificar el usuario propietario nativo de n8n sin exponerlo en argv/logs."""

    return {
        "name": "Configurar propietario inicial de n8n",
        "ansible.builtin.command": {
            "argv": base
            + [
                "exec",
                "-T",
                "app",
                "node",
                "--input-type=module",
                "-e",
                N8N_OWNER_SETUP_SCRIPT,
            ],
            "stdin": base64.b64encode(
                json.dumps(
                    {
                        "email": configuration["owner_email"],
                        "firstName": configuration["owner_first_name"],
                        "lastName": configuration["owner_last_name"],
                        "password": secrets["owner_password"],
                    }
                ).encode()
            ).decode(),
        },
        "register": "n8n_owner_setup",
        "changed_when": "'IACP_N8N_OWNER_CREATED' in n8n_owner_setup.stdout",
        "failed_when": "n8n_owner_setup.rc != 0",
        "no_log": True,
    }


class ServiceBlocked(RuntimeError):
    pass


def service_execution_timeout_seconds(value: str | None = None) -> int:
    """Keep slow package and OCI downloads bounded without failing at 30 minutes."""

    raw = os.getenv("IACP_SERVICE_EXECUTION_TIMEOUT", "10800") if value is None else value
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return 10800
    if seconds < 60:
        return 10800
    return min(seconds, 21600)


def pin_release(service_id, release):
    from api.db import database

    digest = service_release_digest(release)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO service_releases VALUES (?, ?) ON CONFLICT(service_id) DO NOTHING",
            (service_id, digest),
        )
        row = db.execute(
            "SELECT digest FROM service_releases WHERE service_id=?", (service_id,)
        ).fetchone()
        if row["digest"] != digest:
            raise ServiceBlocked("SERVICE_RELEASE_CHANGED")
        db.execute("COMMIT")


def tls_renewal_tasks(base, root, service_id):
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", service_id):
        raise ServiceBlocked("SERVICE_BINDING_IDENTITY_MISMATCH")
    certificate = root + "/pilot.crt"
    key = root + "/pilot.key"
    next_certificate = certificate + ".next"
    next_key = key + ".next"
    previous_certificate = certificate + ".previous"
    previous_key = key + ".previous"

    def inplace_copy(source, destination, mode, *, name="Activar certificado renovado"):
        # Caddy monta los archivos como bind mounts; escribir sobre el inode
        # conserva el montaje visible dentro del contenedor.
        script = (
            "from pathlib import Path\n"
            "import os,sys\n"
            "src=Path(sys.argv[1]); dst=Path(sys.argv[2])\n"
            "data=src.read_bytes()\n"
            "with dst.open('wb') as stream:\n"
            " stream.write(data); stream.flush(); os.fsync(stream.fileno())\n"
            "dst.chmod(int(sys.argv[3],8))\n"
        )
        return {
            "name": name,
            "ansible.builtin.command": {
                "argv": ["python3", "-c", script, source, destination, mode]
            },
            "no_log": True,
        }

    block = [
        {
            "name": "Guardar certificado anterior",
            "ansible.builtin.copy": {
                "src": certificate,
                "dest": previous_certificate,
                "remote_src": True,
                "mode": "0644",
            },
            "no_log": True,
        },
        {
            "name": "Guardar clave anterior",
            "ansible.builtin.copy": {
                "src": key,
                "dest": previous_key,
                "remote_src": True,
                "mode": "0600",
            },
            "no_log": True,
        },
        {
            "name": "Escribir certificado temporal",
            "ansible.builtin.copy": {
                "src": "payload/pilot.crt",
                "dest": next_certificate,
                "mode": "0644",
            },
            "no_log": True,
        },
        {
            "name": "Escribir clave temporal",
            "ansible.builtin.copy": {
                "src": "payload/pilot.key",
                "dest": next_key,
                "mode": "0600",
            },
            "no_log": True,
        },
        inplace_copy(next_certificate, certificate, "0644"),
        inplace_copy(next_key, key, "0600"),
        {"name": "Reiniciar Caddy", **{
            "ansible.builtin.command": {
                "argv": base + ["restart", "caddy"]
            },
            "changed_when": False,
        }},
        {
            "name": "Verificar HTTPS tras renovar certificado",
            "ansible.builtin.uri": {
                "url": "https://{{ ansible_host }}/",
                "validate_certs": True,
                "ca_path": root + "/pilot-ca.crt",
                "status_code": [200],
            },
            "register": "renewed_https_result",
            "until": "renewed_https_result is succeeded",
            "retries": 6,
            "delay": 5,
            "no_log": True,
        },
        {
            "name": "Confirmar activación del certificado renovado",
            "ansible.builtin.set_fact": {"tls_renewal_committed": True},
        },
    ]
    return [
        {
            "name": "Renovar certificado TLS para acceso por IP",
            "block": block,
            "rescue": [
                inplace_copy(
                    previous_certificate,
                    certificate,
                    "0644",
                    name="Restaurar certificado anterior",
                ),
                inplace_copy(
                    previous_key,
                    key,
                    "0600",
                    name="Restaurar clave anterior",
                ),
                {
                    "name": "Restaurar Caddy tras fallo de renovación",
                    "ansible.builtin.command": {
                        "argv": base + ["restart", "caddy"]
                    },
                    "changed_when": False,
                },
                {
                    "name": "Verificar HTTPS restaurado",
                    "ansible.builtin.uri": {
                        "url": "https://{{ ansible_host }}/",
                        "validate_certs": True,
                        "ca_path": root + "/pilot-ca.crt",
                        "status_code": [200],
                    },
                    "register": "restored_https_result",
                    "until": "restored_https_result is succeeded",
                    "retries": 6,
                    "delay": 5,
                    "no_log": True,
                },
                {
                    "name": "Marcar restauración del certificado anterior",
                    "ansible.builtin.set_fact": {"tls_renewal_restored": True},
                },
                {
                    "name": "Informar fallo de renovación TLS",
                    "ansible.builtin.fail": {
                        "msg": "SERVICE_TLS_RENEWAL_VERIFICATION_FAILED"
                    },
                },
            ],
            "always": [
                {
                    "name": "Retirar archivos nuevos de renovación TLS",
                    "ansible.builtin.file": {
                        "path": "{{ item }}",
                        "state": "absent",
                    },
                    "loop": [next_certificate, next_key],
                    "no_log": True,
                },
                {
                    "name": "Retirar copias de seguridad TLS verificadas",
                    "ansible.builtin.file": {
                        "path": "{{ item }}",
                        "state": "absent",
                    },
                    "loop": [previous_certificate, previous_key],
                    "when": "tls_renewal_committed | default(false) or tls_renewal_restored | default(false)",
                    "no_log": True,
                },
            ],
        }
    ]


def execute(service, job, heartbeat):
    if os.getenv("IACP_SERVICE_EXECUTION_ENABLED", "0") != "1":
        raise ServiceBlocked("SERVICE_EXECUTION_DISABLED")
    installing = job["action"] == "install"
    if not installing and job["action"] not in {"handoff", "cleanup", "renew_tls"}:
        raise ServiceBlocked("SERVICE_ACTION_UNSUPPORTED")
    directory = Path(
        os.environ.get("IACP_SERVICE_BINDINGS_DIR", "/etc/iacp/service-bindings")
    )
    try:
        from api.db import database

        with database() as db:
            row = db.execute(
                "SELECT binding_json FROM service_bindings WHERE service_id=?",
                (service["service_id"],),
            ).fetchone()
        binding = (
            json.loads(row["binding_json"])
            if row
            else json.loads((directory / f"{service['instance_id']}.json").read_text())
        )
    except (OSError, ValueError, KeyError):
        raise ServiceBlocked("SERVICE_BINDING_OR_RELEASE_REQUIRED") from None
    if (
        binding.get("instance_id") != service["instance_id"]
        or binding.get("vmid") != service["vmid"]
    ):
        raise ServiceBlocked("SERVICE_BINDING_IDENTITY_MISMATCH")
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        str(service["service_id"]),
    ):
        raise ServiceBlocked("SERVICE_BINDING_IDENTITY_MISMATCH")
    from ipaddress import ip_address

    try:
        address = str(ip_address(binding["address"]))
    except (ValueError, KeyError):
        raise ServiceBlocked("SERVICE_BINDING_ADDRESS_INVALID") from None
    if not re.fullmatch(
        r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+",
        binding.get("host_key", ""),
    ):
        raise ServiceBlocked("SERVICE_HOST_KEY_REQUIRED")
    configuration = json.loads(service["configuration_json"])
    if binding.get("domain") != configuration.get("domain"):
        raise ServiceBlocked("SERVICE_DOMAIN_NOT_APPROVED")
    if installing and configuration.get("domain"):
        import socket

        try:
            addresses = {
                entry[4][0]
                for entry in socket.getaddrinfo(configuration["domain"], 443)
            }
        except OSError:
            raise ServiceBlocked("SERVICE_DNS_NOT_READY") from None
        if address not in addresses:
            raise ServiceBlocked("SERVICE_DNS_ADDRESS_MISMATCH")
    from ipaddress import ip_interface

    spec = json.loads(service.get("spec_json") or "{}")
    if spec.get("ipv4_cidr") and str(ip_interface(spec["ipv4_cidr"]).ip) != address:
        raise ServiceBlocked("SERVICE_ADDRESS_IDENTITY_MISMATCH")
    secrets = decrypt(service["secrets_encrypted"]) if service.get("secrets_encrypted") else {}
    maintenance_access = None
    if job["action"] in {"cleanup", "renew_tls"} and binding.get(
        "maintenance_access_encrypted"
    ):
        try:
            from api.service_secrets import decrypt as decrypt_service_secret

            maintenance_access = decrypt_service_secret(
                binding["maintenance_access_encrypted"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            raise ServiceBlocked("SERVICE_MAINTENANCE_KEY_UNAVAILABLE") from None
    if job["action"] == "renew_tls" or (
        job["action"] == "cleanup" and not secrets
    ):
        if not maintenance_access or not all(
            maintenance_access.get(key)
            for key in ("private_key", "public_key", "installer_public_key")
        ):
            raise ServiceBlocked("SERVICE_MAINTENANCE_KEY_UNAVAILABLE")
        secrets.update(
            ssh_private_key=maintenance_access["private_key"],
            management_ssh_public_key=maintenance_access["public_key"],
            ssh_public_key=maintenance_access["installer_public_key"],
        )
    files = {}
    if installing:
        try:
            release = load_service_release(service["application_id"])
        except (OSError, ValueError, KeyError):
            raise ServiceBlocked("SERVICE_BINDING_OR_RELEASE_REQUIRED") from None
        if not release.get("approved") or release.get("recipe_version") != service["recipe_version"]:
            raise ServiceBlocked("SERVICE_RELEASE_NOT_APPROVED")
        verification = release.get("verification_argv")
        if (
            not isinstance(verification, list)
            or not verification
            or not all(isinstance(arg, str) and arg for arg in verification)
        ):
            raise ServiceBlocked("SERVICE_VERIFICATION_REQUIRED")
        if service["application_id"] == "n8n" and not all((
            configuration.get("owner_email"),
            configuration.get("owner_first_name"),
            configuration.get("owner_last_name"),
            secrets.get("owner_password"),
        )):
            raise ServiceBlocked("SERVICE_N8N_OWNER_REQUIRED")
        if configuration.get("access_mode") == "ip":
            configuration["domain"] = address
        if configuration.get("agent_mode") == "local":
            configuration["model"] = configuration.get("model") or "openrouter/local-validation"
        try:
            compose, files = render(service["application_id"], configuration, secrets, release)
        except RecipeError as error:
            raise ServiceBlocked(str(error)) from None
        if configuration.get("access_mode") == "ip":
            from worker.service_tls import issue

            try:
                files.update(issue(address))
            except (OSError, ValueError):
                raise ServiceBlocked("SERVICE_IP_TLS_NOT_CONFIGURED") from None
        pin_release(service["service_id"], release)
    elif job["action"] == "renew_tls":
        if (
            service["application_id"] not in {"nextcloud", "n8n"}
            or configuration.get("access_mode") != "ip"
        ):
            raise ServiceBlocked("SERVICE_TLS_RENEWAL_UNSUPPORTED")
        configuration["domain"] = address
        from worker.service_tls import issue

        try:
            files.update(issue(address))
            certificate = x509.load_pem_x509_certificate(files["pilot.crt"].encode())
            private_key = serialization.load_pem_private_key(
                files["pilot.key"].encode(), password=None
            )
            san = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.IPAddress)
            if (
                san != [ip_address(address)]
                or certificate.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=5)
                or certificate.public_key().public_numbers() != private_key.public_key().public_numbers()
            ):
                raise ValueError("SERVICE_TLS_RENEWAL_CERTIFICATE_INVALID")
        except (OSError, ValueError):
            raise ServiceBlocked("SERVICE_IP_TLS_NOT_CONFIGURED") from None
    root = f"/opt/iacp-services/{service['service_id']}"
    command = lambda argv: {
        "ansible.builtin.command": {"argv": argv},
        "changed_when": False,
    }
    tasks = [{"name": "Esperar cloud-init", **command(["cloud-init", "status", "--wait", "--long"]),
              "register": "cloud_init", "failed_when": "cloud_init.rc not in [0, 2] or 'status: done' not in cloud_init.stdout"}]
    base = ["docker", "compose", "--project-directory", root]
    if job["action"] == "renew_tls":
        tasks.extend(tls_renewal_tasks(base, root, service["service_id"]))
    elif not installing:
        from worker.service_bootstrap import installer_key_pattern

        if job["action"] == "cleanup":
            tasks.extend([
                {
                    "name": "Comprobar configuración de la aplicación",
                    "ansible.builtin.stat": {"path": root + "/compose.yaml"},
                    "register": "existing_compose",
                    "no_log": True,
                },
                {
                    "name": "Comprobar archivo Compose administrado",
                    "ansible.builtin.assert": {
                        "that": ["not existing_compose.stat.exists or existing_compose.stat.isreg | default(false)"],
                        "fail_msg": "SERVICE_CLEANUP_UNTRUSTED_COMPOSE",
                    },
                },
                {
                    "name": "Retirar contenedores de la aplicación",
                    **command(base + ["down"]),
                    "when": "existing_compose.stat.exists",
                },
                {
                    "name": "Retirar archivos de instalación",
                    "ansible.builtin.file": {"path": root + "/{{ item }}", "state": "absent"},
                    "loop": ["compose.yaml", "Caddyfile", "pilot.key", "pilot.crt", "pilot-ca.crt", "data/config.yaml"],
                    "no_log": True,
                },
            ])
        if job["action"] in {"handoff", "cleanup"}:
            tasks.append(
                {
                    "name": "Retirar clave temporal",
                    "ansible.builtin.lineinfile": {
                        "path": "/root/.ssh/authorized_keys",
                        "regexp": installer_key_pattern(secrets["ssh_public_key"]),
                        "state": "absent",
                    },
                    "no_log": True,
                }
            )
        if job["action"] == "cleanup" and maintenance_access:
            tasks.append(
                {
                    "name": "Retirar clave de mantenimiento",
                    "ansible.builtin.lineinfile": {
                        "path": "/root/.ssh/authorized_keys",
                        "regexp": installer_key_pattern(
                            maintenance_access["public_key"]
                        ),
                        "state": "absent",
                    },
                    "no_log": True,
                }
            )
    else:
        # El host lock evita dos Ansible simultáneos; las pérdidas de worker requieren revisión explícita.
        tasks += [
            {
                "name": "Validar Ubuntu Server 24.04",
                "ansible.builtin.assert": {
                    "that": [
                        "ansible_facts.distribution == 'Ubuntu'",
                        "ansible_facts.distribution_version == '24.04'",
                    ],
                    "fail_msg": "SERVICE_OS_UNSUPPORTED",
                },
            },
            ubuntu_https_sources_task(),
            service_egress_preflight_task(),
            {
                "name": "Instalar dependencias de Ubuntu 24.04",
                "ansible.builtin.apt": {
                    "name": ["docker.io", "docker-compose-v2"],
                    "state": "present",
                    "update_cache": True,
                    "update_cache_retries": 3,
                    "update_cache_retry_max_delay": 30,
                    "lock_timeout": 600,
                },
                "register": "service_packages",
                "until": "service_packages is succeeded",
                "retries": 3,
                "delay": 10,
            },
            {
                "name": "Iniciar Docker",
                "ansible.builtin.service": {
                    "name": "docker",
                    "state": "started",
                    "enabled": True,
                },
            },
            {
                "name": "Crear directorio",
                "ansible.builtin.file": {
                    "path": root,
                    "state": "directory",
                    "mode": "0700",
                },
            },
            {
                "name": "Crear datos",
                "ansible.builtin.file": {
                    "path": root + "/data",
                    "state": "directory",
                    "mode": "0700",
                },
            },
        ]
        files["compose.yaml"] = json.dumps(compose)
        for name, content in files.items():
            tasks.append(
                {
                    "name": "Escribir configuración",
                    "ansible.builtin.copy": {
                        "src": f"payload/{name}",
                        "dest": f"{root}/{name}",
                        "mode": "0600",
                    },
                    "no_log": True,
                }
            )
        tasks.append(
            {
                "name": "Descargar imágenes de aplicación",
                **command(base + ["pull"]),
                "register": "application_image_pull",
                "retries": 3,
                "delay": 10,
                "until": "application_image_pull.rc == 0",
            }
        )
        if service["application_id"] == "openclaw":
            tasks.append(
                {
                    "name": "Configurar gateway",
                    **command(
                        base
                        + [
                            "run",
                            "--rm",
                            "app",
                            "node",
                            "openclaw.mjs",
                            "onboard",
                            "--non-interactive",
                            "--accept-risk",
                            "--skip-daemon",
                            "--skip-health",
                            "--auth-choice",
                            "openrouter-api-key",
                            "--secret-input-mode",
                            "ref",
                        ]
                    ),
                    "no_log": True,
                }
            )
            tasks.append(
                {
                    "name": "Seleccionar modelo",
                    **command(
                        base
                        + [
                            "run",
                            "--rm",
                            "app",
                            "node",
                            "openclaw.mjs",
                            "models",
                            "set",
                            configuration["model"],
                        ]
                    ),
                    "no_log": True,
                }
            )
        up = base + ["up", "-d", "--wait", "--wait-timeout", "300"]
        if service["application_id"] in {"n8n", "nextcloud"}:
            tasks.append({
                "name": "Cerrar proxy durante la configuración inicial",
                **command(base + ["stop", "caddy"]),
            })
        tasks += [
            {
                "name": "Arrancar aplicación",
                **command(
                    up + ["app", "postgres"]
                    if service["application_id"] == "n8n"
                    else up + ["app", "postgres", "redis"]
                    if service["application_id"] == "nextcloud"
                    else up
                ),
            }
        ]
        if service["application_id"] == "n8n":
            tasks.append(n8n_owner_setup_task(base, configuration, secrets))
        if service["application_id"] == "nextcloud":
            tasks.append(
                {
                    "name": "Esperar instalación Nextcloud",
                    **command(nextcloud_status_argv(base)),
                    "register": "installation_status",
                    "failed_when": "installation_status.rc != 0",
                    "no_log": True,
                }
            )
            tasks.extend(
                [
                    {
                        "name": "Autorizar hostname de acceso de Nextcloud",
                        **command(
                            base
                            + [
                                "exec",
                                "-T",
                                "--user",
                                "www-data",
                                "app",
                                "php",
                                "occ",
                                "config:system:set",
                                "trusted_domains",
                                "1",
                                "--value",
                                configuration["domain"],
                            ]
                        ),
                        "no_log": True,
                    },
                    {
                        "name": "Verificar hostname confiable de Nextcloud",
                        **command(
                            base
                            + [
                                "exec",
                                "-T",
                                "--user",
                                "www-data",
                                "app",
                                "php",
                                "occ",
                                "config:system:get",
                                "trusted_domains",
                                "1",
                            ]
                        ),
                        "register": "trusted_domain",
                        "failed_when": "trusted_domain.rc != 0 or trusted_domain.stdout | trim != "
                        + repr(configuration["domain"]),
                        "no_log": True,
                    },
                    {
                        "name": "Resolver proxy",
                        **command(
                            base + ["exec", "-T", "app", "getent", "ahostsv4", "caddy"]
                        ),
                        "register": "proxy_address",
                    },
                    {
                        "name": "Confiar en el proxy asignado",
                        **command(
                            base
                            + [
                                "exec",
                                "-T",
                                "--user",
                                "www-data",
                                "app",
                                "php",
                                "occ",
                                "config:system:set",
                                "trusted_proxies",
                                "0",
                                "--value",
                                "{{ proxy_address.stdout.split()[0] }}",
                            ]
                        ),
                    },
                ]
            )
        if service["application_id"] == "nextcloud":
            tasks.append(
                {
                    "name": "Verificar Nextcloud",
                    **command(nextcloud_status_argv(base)),
                    "register": "nextcloud_status",
                    "failed_when": "nextcloud_status.rc != 0",
                    "no_log": True,
                }
            )
        tasks.append(
            {
                "name": "Verificación funcional revisada",
                **command(base + ["exec", "-T", "app"] + verification),
                "register": "functional_result",
                "until": "functional_result.rc == 0",
                "retries": 30,
                "delay": 2,
                "no_log": True,
            }
        )
        if service["application_id"] in {"n8n", "nextcloud"}:
            tasks.append({
                "name": "Publicar proxy tras verificar la aplicación",
                **command(up + ["caddy"]),
            })
        if configuration.get("domain") and service["application_id"] in {
            "nextcloud",
            "n8n",
        }:
            tasks.append(
                {
                    "name": "Verificar HTTPS",
                    "ansible.builtin.uri": {
                        "url": f"https://{configuration['domain']}/",
                        "validate_certs": True,
                        **({"ca_path": root + "/pilot-ca.crt"} if configuration.get("access_mode") == "ip" else {}),
                        "status_code": [200],
                    },
                    "no_log": True,
                    "register": "https_result",
                    "until": "https_result is succeeded",
                    "retries": 12,
                    "delay": 10,
                }
            )
    with tempfile.TemporaryDirectory(prefix="iacp-service-") as temp:
        folder = Path(temp)
        # Los contenidos del cliente viajan como archivos, nunca como plantillas Jinja.
        for name, content in files.items():
            path = folder / "payload" / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(content)
            path.chmod(0o600)
        key = folder / "key"
        key.write_text(secrets["ssh_private_key"])
        key.chmod(0o600)
        known = folder / "known_hosts"
        known.write_text(f"{address} {binding['host_key']}\n")
        inventory = {
            "all": {
                "hosts": {
                    "target": {
                        "ansible_host": address,
                        "ansible_user": "root",
                        "ansible_ssh_private_key_file": str(key),
                        "ansible_ssh_common_args": f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known} -o IdentitiesOnly=yes",
                    }
                }
            }
        }
        (folder / "inventory.json").write_text(json.dumps(inventory))
        # La revocación de la clave es idempotente. Una pérdida durante el handoff requiere
        # comprobar su ausencia por el canal del operador antes de cerrar la entrega.
        # La identidad remota permite retirar solo el bloqueo de una ejecución
        # anterior cuya exclusión local ya se adquirió. Así una interrupción de
        # Ansible no deja un directorio vacío que bloquee todos los reintentos.
        lock = "/var/lib/iacp-service-install.lock"
        owner = {
            "service_id": service["service_id"],
            "job_id": job["job_id"],
            "generation": job["generation"],
        }
        previous_job_id = str(job.get("previous_job_id") or "")
        acquire_lock_script = (
            "import json,os,sys\n"
            "from pathlib import Path\n"
            "lock=Path(sys.argv[1]); current=json.loads(sys.argv[2]); previous=sys.argv[3]\n"
            "if lock.exists() or lock.is_symlink():\n"
            " if lock.is_symlink() or not lock.is_dir(): raise SystemExit(73)\n"
            " owner_path=lock/'owner.json'\n"
            " try: old=json.loads(owner_path.read_text())\n"
            " except FileNotFoundError: old=None\n"
            " except (OSError,ValueError): raise SystemExit(73)\n"
            " entries=list(lock.iterdir())\n"
            " if old is None:\n"
            "  if entries or (current['generation'] <= 1 and not previous): raise SystemExit(73)\n"
            " elif not (old.get('service_id')==current['service_id'] and ((old.get('job_id')==current['job_id'] and type(old.get('generation')) is int and old['generation']<current['generation']) or old.get('job_id')==previous)):\n"
            "  raise SystemExit(73)\n"
            " if old is not None and entries != [owner_path]: raise SystemExit(73)\n"
            " if old is not None: owner_path.unlink()\n"
            " lock.rmdir()\n"
            "os.mkdir(lock,0o700)\n"
            "with (lock/'owner.json').open('x') as stream:\n"
            " json.dump(current,stream,separators=(',',':')); stream.flush(); os.fsync(stream.fileno())\n"
        )
        release_lock_script = (
            "import json,sys\n"
            "from pathlib import Path\n"
            "lock=Path(sys.argv[1]); expected=json.loads(sys.argv[2]); owner=lock/'owner.json'\n"
            "if not lock.exists() and not lock.is_symlink(): raise SystemExit(0)\n"
            "if lock.is_symlink() or not lock.is_dir(): raise SystemExit(73)\n"
            "try: actual=json.loads(owner.read_text())\n"
            "except (OSError,ValueError): raise SystemExit(73)\n"
            "if actual != expected or list(lock.iterdir()) != [owner]: raise SystemExit(73)\n"
            "owner.unlink(); lock.rmdir()\n"
        )
        owner_json = json.dumps(owner, separators=(",", ":"))
        lock_tasks = [{
            "name": "Adquirir exclusión remota",
            **command(["python3", "-c", acquire_lock_script, lock, owner_json, previous_job_id]),
        }]
        cleanup = [{
            "name": "Liberar exclusión remota",
            **command(["python3", "-c", release_lock_script, lock, owner_json]),
        }]
        playbook = [
            {
                "hosts": "all",
                # La KVM puede haber terminado Terraform antes de que SSH
                # termine de arrancar. Esperar conectividad antes de recopilar
                # facts evita convertir esa ventana normal en una instalación
                # incierta sin haber ejecutado cambios en el invitado.
                "gather_facts": False,
                "pre_tasks": [
                    {
                        "name": "Esperar conectividad SSH",
                        "ansible.builtin.wait_for_connection": {
                            "connect_timeout": 15,
                            "delay": 5,
                            "sleep": 5,
                            "timeout": 600,
                        },
                    },
                    {
                        "name": "Recopilar facts del invitado",
                        "ansible.builtin.setup": {"gather_subset": ["min"]},
                    },
                ],
                "tasks": [{"block": lock_tasks + tasks, "always": cleanup}],
            }
        ]
        (folder / "playbook.json").write_text(json.dumps(playbook))
        for path in folder.iterdir():
            path.chmod(0o700 if path.is_dir() else 0o600)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "HOME"}
        }
        environment.update(
            ANSIBLE_LOCAL_TEMP=str(folder / "ansible"),
            ANSIBLE_SSH_CONTROL_PATH_DIR=str(folder / "ssh"),
            ANSIBLE_CALLBACK_PLUGINS=str(
                Path(__file__).resolve().parent / "ansible_callbacks"
            ),
            ANSIBLE_STDOUT_CALLBACK="iacp_safe_summary",
            ANSIBLE_LOAD_CALLBACK_PLUGINS="1",
            ANSIBLE_HOST_KEY_CHECKING="True",
            ANSIBLE_NOCOLOR="1",
        )
        summary_path = folder / "safe-ansible-events.log"
        with summary_path.open("w", encoding="utf-8") as output:
            summary_path.chmod(0o600)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ansible.cli.playbook",
                    "-i",
                    str(folder / "inventory.json"),
                    str(folder / "playbook.json"),
                ],
                env=environment,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(
                    (lock_fd,) if (lock_fd := current_service_job_lock_fd()) is not None else ()
                ),
            )
            import time

            started = time.monotonic()
            execution_timeout = service_execution_timeout_seconds()
            timed_out = False
            try:
                while process.poll() is None:
                    heartbeat()
                    if time.monotonic() - started > execution_timeout:
                        timed_out = True
                        break
                    time.sleep(2)
            finally:
                if process.poll() is None:
                    import signal

                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
            output.flush()
            if timed_out or process.returncode:
                error = safe_execution_error(
                    summary_path.read_text(encoding="utf-8", errors="replace"),
                    return_code=process.returncode,
                    timed_out=timed_out,
                )
                failure = (
                    ServiceExecutionFailed
                    if error.startswith("SERVICE_EXECUTION_FAILED:")
                    else ServiceExecutionUncertain
                )
                raise failure(error)
