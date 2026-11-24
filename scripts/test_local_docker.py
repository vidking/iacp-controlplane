#!/usr/bin/env python3
"""Construir y comprobar el runtime 6+6 en Docker local, sin conectarse a MNG."""

import argparse
import json
import subprocess
import tempfile
import uuid
from pathlib import Path
import go_live

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, input=None, check=True):
    result = subprocess.run(command, input=input, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(
            f"Falló {command[0]} {command[1]} (código {result.returncode})"
        )
    return result


def validate(image, evidence):
    project = "iacp-local-check-" + uuid.uuid4().hex[:8]
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    test_image = project + ":tests"
    service_volume = project + "-service-config"
    api_volume = project + "-api-config"
    infra_volume = project + "-infra-key"
    compose = evidence / "compose.json"
    env_file = evidence / "runtime.env"
    env_file.write_text("")
    env_file.chmod(0o600)
    command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "-f",
        str(compose),
        "--profile",
        "services",
        "--profile",
        "service-workers-3-4",
        "--profile",
        "service-workers-5-6",
    ]

    def cp(*args, check=True):
        return run(command + list(args), check=check)

    environment = {
        "PGHOST": "postgres",
        "PGPORT": "5432",
        "PGDATABASE": "iacp_test",
        "PGUSER": "postgres",
        "PGPASSWORD": "local-only",
        "IACP_BUILD_REVISION": "local-uncommitted-validation",
        "IACP_RUNTIME_STAGE": "develop",
        "IACP_API_TOKEN": "local-test-only-api-token-not-a-secret",
        "IACP_DEFAULT_ENVIRONMENT": "lab",
        "IACP_ENABLED_ENVIRONMENTS": "lab",
        "IACP_WASABI_ENABLED": "false",
        "IACP_MAX_CONCURRENT": "6",
        "IACP_MAX_QUEUE_DEPTH": "100",
        "IACP_VMID_MIN": "40000",
        "IACP_VMID_MAX": "49999",
        "IACP_SERVICES_ENABLED": "1",
        "IACP_SERVICE_EXECUTION_ENABLED": "0",
        "IACP_SERVICE_MAX_CONCURRENT": "6",
        "IACP_SERVICE_KEY_FILE": "/run/iacp-services/key",
        "IACP_PROXMOX_MNG_ENDPOINT": "https://mng.test.invalid:8006/",
        "IACP_PROXMOX_MNG_API_TOKEN": "local-test-only-no-valid-credential",
        "IACP_PROXMOX_MNG_POOL_ID": "local-test",
        "IACP_WORKER_IDLE_HEARTBEAT_SECONDS": "1",
        "IACP_PROD_STATIC_ENABLED": "0",
    }
    common = {
        "image": image,
        "read_only": True,
        "tmpfs": ["/tmp:rw,nosuid,size=128m"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "environment": environment,
        "volumes": ["workdata:/var/lib/iacp-controlplane"],
    }
    services = {
        "postgres": {
            "image": go_live.POSTGRES_IMAGE,
            "environment": {
                "POSTGRES_DB": "iacp_test",
                "POSTGRES_HOST_AUTH_METHOD": "trust",
            },
            "volumes": ["database:/var/lib/postgresql/data"],
            "healthcheck": {
                "test": ["CMD", "pg_isready", "-U", "postgres", "-d", "iacp_test"],
                "interval": "2s",
                "timeout": "2s",
                "retries": 30,
            },
        }
    }
    services["api"] = dict(
        common,
        environment=environment | {
            "IACP_SERVICE_KEY_FILE": "/run/iacp-services/key",
            "IACP_SERVICE_CA_CERT_FILE": "/run/iacp-services/pilot-ca.crt",
            "IACP_SERVICE_CA_KEY_FILE": "/run/iacp-services/pilot-ca.key",
        },
        volumes=common["volumes"] + ["api-config:/run/iacp-services:ro"],
        healthcheck={
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready')",
            ],
            "interval": "2s",
            "timeout": "3s",
            "retries": 30,
        },
        depends_on={"postgres": {"condition": "service_healthy"}},
    )
    for i in range(1, 7):
        services[f"worker-{i}"] = dict(
            common,
            volumes=common["volumes"] + ["infra-key:/run/iacp-services:ro"],
            command=["python", "-m", "worker.main", "--worker-id", f"worker-{i}"],
            depends_on={"api": {"condition": "service_healthy"}},
        )
    for i in range(1, 7):
        env = {
            k: v
            for k, v in environment.items()
            if k.startswith("PG") or k.startswith("IACP_SERVICE")
        }
        services[f"service-worker-{i}"] = dict(
            common,
            environment=env,
            volumes=[
                "service-config:/run/iacp-services:ro",
                "workdata:/var/lib/iacp-controlplane",
            ],
            profiles=(
                ["services"]
                if i <= 2
                else ["service-workers-3-4"]
                if i <= 4
                else ["service-workers-5-6"]
            ),
            command=[
                "python",
                "-m",
                "worker.services",
                "--worker-id",
                f"service-worker-{i}",
            ],
            depends_on={"api": {"condition": "service_healthy"}},
        )
    services["test-runner"] = {
        "image": test_image,
        "init": True,
        "profiles": ["checks"],
        "read_only": True,
        "tmpfs": ["/tmp:rw,nosuid,size=512m"],
        "environment": {
            "IACP_DATABASE_URL": "postgresql://postgres@postgres:5432/iacp_test",
            "IACP_TEST_DATABASE_URL": "postgresql://postgres@postgres:5432/iacp_test"
        },
        "command": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
    }
    definition = {
        "services": services,
        "volumes": {
            "workdata": {},
            "database": {},
            "service-config": {"external": True, "name": service_volume},
            "api-config": {"external": True, "name": api_volume},
            "infra-key": {"external": True, "name": infra_volume},
        },
        "networks": {"default": {"internal": True}},
    }
    compose.write_text(json.dumps(definition))
    compose.chmod(0o600)
    try:
        result = run(
            ["docker", "build", "--progress=plain", "-t", image, str(ROOT)], check=False
        )
        (evidence / "build.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Falló la construcción de la imagen del runtime")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".Dockerfile") as dockerfile:
            dockerfile.write(f"""FROM {image}
USER root
RUN uv sync --locked --extra dev --no-install-project
COPY tests /app/tests
COPY scripts /app/scripts
COPY compose.yaml /app/compose.yaml
COPY deploy/portainer/docker-compose.yml /app/deploy/portainer/docker-compose.yml
COPY deploy/portainer/traefik-dev.yml /app/deploy/portainer/traefik-dev.yml
COPY docs /app/docs
COPY deploy/portainer/manifest-dev.example.json /app/deploy/portainer/manifest-dev.example.json
COPY deploy/portainer/manifest-prod.example.json /app/deploy/portainer/manifest-prod.example.json
USER iacp
""")
            dockerfile.flush()
            result = run(
                [
                    "docker",
                    "build",
                    "--progress=plain",
                    "-t",
                    test_image,
                    "-f",
                    dockerfile.name,
                    str(ROOT),
                ],
                check=False,
            )
        (evidence / "test-build.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Falló la construcción de la imagen de pruebas")
        for volume in (service_volume, api_volume, infra_volume):
            run(["docker", "volume", "create", volume])
        result = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "root",
                "-v",
                service_volume + ":/service",
                "-v",
                api_volume + ":/api",
                "-v",
                infra_volume + ":/infra",
                test_image,
                "python",
                "-c",
                "from pathlib import Path; import os,shutil; from cryptography.fernet import Fernet; from scripts.prepare_pilot_ca import prepare; s=Path('/service'); a=Path('/api'); i=Path('/infra'); [(os.chmod(p,0o700),os.chown(p,999,999)) for p in (s,a,i)]; prepare(s,uid=999,gid=999); key=Fernet.generate_key(); [(p/'key').write_bytes(key) for p in (s,a,i)]; [(os.chmod(p/'key',0o600),os.chown(p/'key',999,999)) for p in (s,a,i)]; shutil.copyfile(s/'pilot-ca.crt',a/'pilot-ca.crt'); shutil.copyfile(s/'pilot-ca.key',a/'pilot-ca.key'); [(os.chmod(a/name,0o600),os.chown(a/name,999,999)) for name in ('key','pilot-ca.key')]; os.chmod(a/'pilot-ca.crt',0o644); os.chown(a/'pilot-ca.crt',999,999); (s/'pilot-ca.crt').unlink(); (s/'pilot-ca.key').unlink()",
            ],
            check=False,
        )
        (evidence / "config-seed.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Falló la preparación de claves de prueba")
        result = cp("up", "-d", "--wait", "--wait-timeout", "90", check=False)
        (evidence / "startup.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Falló el arranque del stack aislado")
        manifest = {
            "project": project,
            "image_tag": "local-uncommitted-validation",
            "schema_version": 3,
            "services_enabled": True,
            "service_worker_count": 6,
        }
        verification = go_live._verify(manifest, compose, env_file)
        (evidence / "runtime.json").write_text(json.dumps(verification, indent=2))
        result = cp("run", "--rm", "--no-deps", "test-runner", check=False)
        (evidence / "pytest.log").write_text(result.stdout + result.stderr)
        print(result.stdout)
        if result.returncode:
            raise RuntimeError("La suite local terminó con errores")
        image_info = json.loads(run(["docker", "image", "inspect", image]).stdout)[0]
        summary = {
            "result": "PASS",
            "scope": "Docker local; no certificación física MNG",
            "image_id": image_info["Id"],
            "runtime": verification["api"],
            "pytest_exit_code": result.returncode,
        }
        (evidence / "summary.json").write_text(json.dumps(summary, indent=2))
        print(f"PASS: runtime local aislado y suite unitaria. Evidencia: {evidence}")
    finally:
        logs = cp("logs", "--no-color", check=False)
        (evidence / "containers.log").write_text(logs.stdout + logs.stderr)
        volumes = run(
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ],
            check=False,
        ).stdout.split()
        cp("down", check=False)
        for volume in [*volumes, service_volume, api_volume, infra_volume]:
            run(["docker", "volume", "rm", volume], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="iacp-controlplane:dev-validation")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path(tempfile.gettempdir())
        / ("iacp-dev-validation-" + uuid.uuid4().hex[:8]),
    )
    args = parser.parse_args()
    if not __import__("re").fullmatch(r"[a-z0-9][a-z0-9._:/-]*", args.image):
        parser.error("Referencia de imagen inválida")
    try:
        validate(args.image, args.evidence_dir.resolve())
    except (OSError, RuntimeError, go_live.GateError) as error:
        parser.exit(1, f"FAIL: {error}. Consulte {args.evidence_dir}\n")


if __name__ == "__main__":
    main()
