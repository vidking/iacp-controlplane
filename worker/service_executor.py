"""Ejecutor Ansible. Los bindings SSH y releases son entradas del operador, no del consumidor."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from api.service_secrets import decrypt
from worker.service_recipes import render, RecipeError
from worker.service_diagnostics import (
    ServiceExecutionFailed,
    ServiceExecutionUncertain,
    safe_execution_error,
)


NEXTCLOUD_READY_SCRIPT = (
    "set -eu\n"
    "attempt=0\n"
    "while [ \"$attempt\" -lt 60 ]; do\n"
    "  if php occ status --output=json 2>/dev/null | grep -Eq '\"installed\"[[:space:]]*:[[:space:]]*true[[:space:]]*([,}])'; then\n"
    "    printf 'IACP_NEXTCLOUD_INSTALLED\\n'\n"
    "    exit 0\n"
    "  fi\n"
    "  attempt=$((attempt + 1))\n"
    "  sleep 5\n"
    "done\n"
    "exit 1\n"
)

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
if (setup.status === 200 && setup.body.email === account.email) {
  console.log('IACP_N8N_OWNER_CREATED');
  process.exit(0);
}
// Si el worker reanuda después de que n8n creó la cuenta, verificar el login
// convierte la repetición en idempotente sin revelar la causa ni la respuesta.
const login = await post('/login', {
  emailOrLdapLoginId: account.email,
  password: account.password,
});
if (login.status === 200 && login.body.email === account.email) {
  console.log('IACP_N8N_OWNER_VERIFIED');
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
    """Espera a que OCC informe instalación sin analizar JSON intermedio inválido."""

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


def pin_release(service_id, release):
    import hashlib
    from api.db import database

    digest = hashlib.sha256(json.dumps(release, sort_keys=True).encode()).hexdigest()
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


def execute(service, job, heartbeat):
    if os.getenv("IACP_SERVICE_EXECUTION_ENABLED", "0") != "1":
        raise ServiceBlocked("SERVICE_EXECUTION_DISABLED")
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
        release = json.loads(
            Path(
                os.getenv(
                    "IACP_SERVICE_RELEASES_FILE",
                    str(Path(__file__).resolve().parents[1] / "services/releases.json"),
                )
            ).read_text()
        )[service["application_id"]]
    except (OSError, ValueError, KeyError):
        raise ServiceBlocked("SERVICE_BINDING_OR_RELEASE_REQUIRED") from None
    if (
        binding.get("instance_id") != service["instance_id"]
        or binding.get("vmid") != service["vmid"]
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
    if configuration.get("domain"):
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
    if (
        not release.get("approved")
        or release.get("recipe_version") != service["recipe_version"]
    ):
        raise ServiceBlocked("SERVICE_RELEASE_NOT_APPROVED")
    verification = release.get("verification_argv")
    if (
        not isinstance(verification, list)
        or not verification
        or not all(isinstance(arg, str) and arg for arg in verification)
    ):
        raise ServiceBlocked("SERVICE_VERIFICATION_REQUIRED")
    if configuration.get("access_mode") == "ip":
        configuration["domain"] = address
    if configuration.get("agent_mode") == "local":
        configuration["model"] = configuration.get("model") or "openrouter/local-validation"
    secrets = decrypt(service["secrets_encrypted"])
    try:
        compose, files = render(
            service["application_id"], configuration, secrets, release
        )
    except RecipeError as error:
        raise ServiceBlocked(str(error)) from None
    if configuration.get("access_mode") == "ip":
        from worker.service_tls import issue
        try:
            files.update(issue(address))
        except (OSError, ValueError):
            raise ServiceBlocked("SERVICE_IP_TLS_NOT_CONFIGURED") from None
    pin_release(service["service_id"], release)
    root = f"/opt/iacp-services/{service['service_id']}"
    command = lambda argv: {
        "ansible.builtin.command": {"argv": argv},
        "changed_when": False,
    }
    tasks = [{"name": "Esperar cloud-init", **command(["cloud-init", "status", "--wait", "--long"]),
              "register": "cloud_init", "failed_when": "cloud_init.rc not in [0, 2] or 'status: done' not in cloud_init.stdout"}]
    if job["action"] == "handoff":
        tasks += [
            {
                "name": "Retirar clave temporal",
                "ansible.builtin.lineinfile": {
                    "path": "/root/.ssh/authorized_keys",
                    "line": secrets["ssh_public_key"],
                    "state": "absent",
                },
                "no_log": True,
            }
        ]
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
        base = ["docker", "compose", "--project-directory", root]
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
        tasks += [
            {
                "name": "Arrancar aplicación",
                **command(base + ["up", "-d", "--wait", "--wait-timeout", "300"]),
            }
        ]
        if service["application_id"] == "n8n" and secrets.get("owner_password"):
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
        # Bloqueo persistente en el invitado, con generación explícita. No expira automáticamente.
        lock = "/var/lib/iacp-service-install.lock"
        lock_tasks = [{"name": "Adquirir exclusión remota", **command(["mkdir", lock])}]
        cleanup = [{"name": "Liberar exclusión remota", **command(["rmdir", lock])}]
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
                "tasks": lock_tasks + [{"block": tasks, "always": cleanup}],
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
            )
            import time

            started = time.monotonic()
            timed_out = False
            try:
                while process.poll() is None:
                    heartbeat()
                    if time.monotonic() - started > 1800:
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
