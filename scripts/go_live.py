#!/usr/bin/env python3
"""Operación segura de los stacks V1 del IACP Control Plane."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import selectors
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import yaml


ROOT = Path(__file__).resolve().parents[1]
LEGACY_VOLUMES = {
    "iacp-controlplane_postgres_data",
    "iacp-controlplane_redis_data",
    "iacp-controlplane_terraform_plans",
    "iacp-controlplane_technical_data",
    "iacp-controlplane_technical_postgres_data",
}
COMMIT_TAG = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class GateError(RuntimeError):
    pass


def _run(command: list[str], *, label: str, timeout: int = 120) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise GateError(f"{label} terminó con código {result.returncode}")
    return result.stdout


def _manifest(path: Path, *, require_runtime: bool = True) -> tuple[dict, Path, Path]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GateError("No se pudo leer el manifiesto") from error
    required = {
        "project", "compose_file", "environment_file", "stage", "domain",
        "service_alias", "wasabi_enabled", "backend", "state_bucket", "state_prefix",
        "max_concurrent", "vmid_min", "vmid_max", "dynamic_config", "proxy_network",
    }
    if not isinstance(data, dict) or not required.issubset(data):
        raise GateError("El manifiesto no contiene los campos requeridos")
    compose = (ROOT / data["compose_file"]).resolve()
    env_file = Path(data["environment_file"]).resolve()
    if require_runtime and (ROOT not in compose.parents or not compose.is_file()):
        raise GateError("El Compose no pertenece al release o no existe")
    if require_runtime and not env_file.is_file():
        raise GateError("No existe el archivo privado de configuración")
    return data, compose, env_file


def _compose_args(data: dict, compose: Path, env_file: Path) -> list[str]:
    profiles = []
    if data.get("services_enabled", False):
        profiles.extend(["--profile", "services"])
        worker_count = len(_service_worker_names(data))
        if worker_count > 2:
            profiles.extend(["--profile", "service-workers-3-4"])
        if worker_count > 4:
            profiles.extend(["--profile", "service-workers-5-6"])
    return [
        "docker", "compose", "--project-name", data["project"],
        "--env-file", str(env_file), "-f", str(compose),
    ] + profiles


def _service_worker_names(data: dict) -> tuple[str, ...]:
    if not data.get("services_enabled", False):
        return ()
    # Desarrollo y producción operan con seis workers de infraestructura y seis
    # de servicios; un pool menor ya no se acepta ni por omisión en el manifiesto.
    count = data.get("service_worker_count", 6)
    if type(count) is not int or count != 6:
        raise GateError("service_worker_count debe ser 6 (seis workers de servicios por stack)")
    return tuple(f"service-worker-{number}" for number in range(1, count + 1))


def _compose_config(data: dict, compose: Path, env_file: Path) -> dict:
    raw = _run(_compose_args(data, compose, env_file) + ["config", "--format", "json"], label="Compose config")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise GateError("Compose no devolvió configuración JSON válida") from error


def _service_env(config: dict, name: str) -> dict:
    service = config.get("services", {}).get(name)
    if not isinstance(service, dict):
        raise GateError(f"Falta el servicio {name}")
    environment = service.get("environment", {})
    if isinstance(environment, list):
        return dict(item.split("=", 1) if "=" in item else (item, "") for item in environment)
    return environment if isinstance(environment, dict) else {}


def _extra_host_map(service: dict) -> dict[str, str]:
    configured = service.get("extra_hosts", [])
    if isinstance(configured, dict):
        return {str(host): str(address) for host, address in configured.items()}
    result = {}
    for entry in configured if isinstance(configured, list) else []:
        value = str(entry)
        separator = "=" if "=" in value else ":"
        if separator in value:
            host, address = value.split(separator, 1)
            result[host.strip()] = address.strip()
    return result


def _has_readonly_bind_mount(service: dict, *, source: str, target: str) -> bool:
    for mount in service.get("volumes", []):
        if isinstance(mount, dict):
            if (
                mount.get("type") == "bind"
                and str(mount.get("source", "")) == source
                and mount.get("target") == target
                and mount.get("read_only") is True
            ):
                return True
            continue
        parts = str(mount).split(":")
        if len(parts) >= 3 and parts[-2] == target and parts[0] == source and parts[-1] == "ro":
            return True
    return False


def _bind_mount_overlaps(path: str, protected_directory: Path) -> bool:
    source = Path(path).resolve(strict=False)
    protected = protected_directory.resolve(strict=False)
    return (
        source == protected
        or source in protected.parents
        or protected in source.parents
    )


def _private_environment(path: Path) -> dict:
    try:
        from scripts.envfile import read_env_file
    except ModuleNotFoundError:
        from envfile import read_env_file
    if not path.is_file() or (path.stat().st_mode & 0o777) != 0o600:
        raise GateError("El entorno de referencia debe existir y tener permisos 600")
    return read_env_file(path)


def _check_development_credentials(data: dict, env_file: Path, config: dict) -> None:
    if data.get("stage") != "develop":
        return
    reference = data.get("isolation_reference_environment_file")
    if not reference:
        raise GateError("Falta el entorno productivo de referencia para verificar aislamiento")
    production = _private_environment(Path(reference))
    development = _private_environment(env_file)
    actual = {"POSTGRES_PASSWORD": _service_env(config, "postgres").get("POSTGRES_PASSWORD"),
              "IACP_API_TOKEN": _service_env(config, "api").get("IACP_API_TOKEN"),
              "IACP_PROXMOX_MNG_API_TOKEN": _service_env(config, "api").get("IACP_PROXMOX_MNG_API_TOKEN")}
    shared_mng = data.get("mng_credentials_shared", False)
    if not isinstance(shared_mng, bool):
        raise GateError("mng_credentials_shared debe ser booleano")
    for key in ("POSTGRES_PASSWORD", "IACP_API_TOKEN", "IACP_PROXMOX_MNG_API_TOKEN"):
        if actual.get(key) != development.get(key):
            raise GateError(f"El entorno efectivo no coincide con el archivo privado: {key}")
        if not development.get(key) or not production.get(key) or (development[key] == production[key] and not (shared_mng and key == "IACP_PROXMOX_MNG_API_TOKEN")):
            raise GateError(f"Desarrollo requiere una credencial exclusiva: {key}")


def _check_policy(data: dict, config: dict, env_file: Path) -> dict:
    stage = data["stage"]
    if stage not in {"production", "develop"}:
        raise GateError("El manifiesto debe identificar production o develop")
    expected_stage = "production" if stage == "production" else "develop"
    expected_identity = {
        "production": {
            "project": "iacp-controlplane-prod-v1",
            "domain": "iac.cyberfuel.com",
            "service_alias": "iacp-controlplane-api-prod",
            "postgres_volume": "iacp_controlplane_prod_postgres",
            "workdata_volume": "iacp_controlplane_prod_workdata",
            "database": "iacp_controlplane",
            "vmid_min": 30000,
            "vmid_max": 39999,
        },
        "develop": {
            "project": "iacp-controlplane-dev-v1",
            "domain": "dev-iac.cyberfuel.com",
            "service_alias": "iacp-controlplane-api-dev",
            "postgres_volume": "iacp_controlplane_dev_postgres",
            "workdata_volume": "iacp_controlplane_dev_workdata",
            "database": "iacp_controlplane_dev",
            "vmid_min": 40000,
            "vmid_max": 49999,
        },
    }[stage]
    for key in ("project", "domain", "service_alias"):
        if data[key] != expected_identity[key]:
            raise GateError(f"El manifiesto no coincide con la identidad autorizada de {stage}")
    expected_dynamic_config = Path(
        "/etc/iacp/tls/dynamic/iacp.yml"
        if stage == "production"
        else "/etc/iacp/tls/dynamic/dev-iac.yml"
    )
    if Path(data.get("dynamic_config", "")) != expected_dynamic_config:
        raise GateError("La ruta dinámica de Traefik no coincide con el entorno autorizado")
    expected_vmid_range = (expected_identity["vmid_min"], expected_identity["vmid_max"])
    if (data.get("vmid_min"), data.get("vmid_max")) != expected_vmid_range:
        raise GateError(f"El rango VMID no coincide con el entorno {stage}")
    if data["proxy_network"] != "proxy_network":
        raise GateError("La red compartida debe ser proxy_network")
    api_env = _service_env(config, "api")
    worker_names = tuple(f"worker-{number}" for number in range(1, 7))
    services_enabled = data.get("services_enabled", False)
    if not isinstance(services_enabled, bool):
        raise GateError("services_enabled debe ser booleano")
    schema_version = data.get("schema_version", 2)
    if type(schema_version) is not int or schema_version not in {2, 3}:
        raise GateError("schema_version debe ser 2 o 3")
    if services_enabled and schema_version != 3:
        raise GateError("Los servicios requieren esquema 3")
    service_worker_names = _service_worker_names(data)
    app_names = ("api", *worker_names, *service_worker_names)
    actual_names = set(config.get("services", {}))
    if actual_names != {"postgres", *app_names}:
        raise GateError("El inventario Compose no coincide con los pools declarados")
    flag = str(api_env.get("IACP_SERVICES_ENABLED", "0")).lower() in {"1", "true", "yes"}
    if flag != services_enabled:
        raise GateError("IACP_SERVICES_ENABLED no coincide con el manifiesto")
    if services_enabled and stage == "production" and str(api_env.get("IACP_ENFORCE_WAN_ONLY", "0")) != "1":
        raise GateError("La promoción definitiva requiere IACP_ENFORCE_WAN_ONLY=1")
    if services_enabled:
        execution = data.get("service_execution_enabled", False)
        if not isinstance(execution, bool):
            raise GateError("service_execution_enabled debe ser booleano")
        runtime_environment = _private_environment(env_file)
        service_config_dir = runtime_environment.get("IACP_SERVICE_CONFIG_DIR", "")
        if not service_config_dir or not Path(service_config_dir).is_absolute():
            raise GateError("IACP_SERVICE_CONFIG_DIR debe apuntar a un directorio absoluto")
        service_directory = Path(service_config_dir)
        if service_directory.is_symlink() or not service_directory.is_dir():
            raise GateError("El directorio de servicios debe existir y no ser un enlace")
        service_key = service_directory / "key"
        if (
            not service_key.is_file()
            or service_key.is_symlink()
            or (service_key.stat().st_mode & 0o077)
        ):
            raise GateError("La clave de servicios debe existir como archivo privado")
        service_ca_key = service_directory / "pilot-ca.key"
        service_ca_cert = service_directory / "pilot-ca.crt"
        if (
            not service_ca_key.is_file()
            or service_ca_key.is_symlink()
            or (service_ca_key.stat().st_mode & 0o077)
        ):
            raise GateError("La clave privada de la CA debe existir como archivo privado")
        if not service_ca_cert.is_file() or service_ca_cert.is_symlink():
            raise GateError("El certificado público de la CA no existe")
        if (service_ca_cert.stat().st_mode & 0o022):
            raise GateError("El certificado público de la CA no debe ser modificable por grupo u otros")
        if api_env.get("IACP_SERVICE_KEY_FILE") != "/run/iacp-service-key":
            raise GateError("La API debe recibir la clave Fernet por un montaje de archivo dedicado")
        if api_env.get("IACP_SERVICE_CA_CERT_FILE") != "/run/iacp-pilot-ca.crt":
            raise GateError("La API debe recibir el certificado público de la CA")
        if api_env.get("IACP_SERVICE_CA_KEY_FILE") != "/run/iacp-pilot-ca.key":
            raise GateError("La API debe recibir la clave privada de firma por un montaje dedicado")
        api_service = config["services"]["api"]
        if not _has_readonly_bind_mount(
            api_service,
            source=str(service_key),
            target="/run/iacp-service-key",
        ) or not _has_readonly_bind_mount(
            api_service,
            source=str(service_ca_cert),
            target="/run/iacp-pilot-ca.crt",
        ) or not _has_readonly_bind_mount(
            api_service,
            source=str(service_ca_key),
            target="/run/iacp-pilot-ca.key",
        ):
            raise GateError("La API requiere montajes individuales y de solo lectura de las claves de servicio y CA")
        allowed_api_mounts = {
            (service_key.resolve(strict=False), "/run/iacp-service-key"),
            (service_ca_cert.resolve(strict=False), "/run/iacp-pilot-ca.crt"),
            (service_ca_key.resolve(strict=False), "/run/iacp-pilot-ca.key"),
        }
        for mount in api_service.get("volumes", []):
            if not isinstance(mount, dict) or mount.get("type") != "bind":
                continue
            mount_source = str(mount.get("source", ""))
            if _bind_mount_overlaps(mount_source, service_directory) and (
                Path(mount_source).resolve(strict=False), mount.get("target")
            ) not in allowed_api_mounts:
                raise GateError("La API no debe montar el directorio completo de servicios")
        for name in worker_names:
            env = _service_env(config, name)
            service = config["services"][name]
            if env.get("IACP_SERVICE_KEY_FILE") != "/run/iacp-services/key":
                raise GateError(f"Falta IACP_SERVICE_KEY_FILE en {name}")
            if not _has_readonly_bind_mount(
                service,
                source=str(service_key),
                target="/run/iacp-services/key",
            ):
                raise GateError(f"La clave de servicios debe montarse en solo lectura en {name}")
            for mount in service.get("volumes", []):
                if not isinstance(mount, dict) or mount.get("type") != "bind":
                    continue
                mount_source = str(mount.get("source", ""))
                if (
                    _bind_mount_overlaps(mount_source, service_directory)
                    and Path(mount_source).resolve(strict=False)
                    != service_key.resolve(strict=False)
                ):
                    raise GateError(f"{name} no debe recibir la clave privada de la CA")
        for name in service_worker_names:
            env = _service_env(config, name)
            service_worker = config["services"][name]
            if str(env.get("IACP_SERVICES_ENABLED", "0")) != "1":
                raise GateError("Todos los workers de servicios deben estar habilitados")
            if (str(env.get("IACP_SERVICE_EXECUTION_ENABLED", "0")) == "1") != execution:
                raise GateError("El permiso de ejecución de servicios no coincide")
            if str(env.get("IACP_SERVICE_MAX_CONCURRENT", "2")) != str(len(service_worker_names)):
                raise GateError("La concurrencia configurada no coincide con el pool de servicios")
            if env.get("IACP_SERVICE_KEY_FILE") != "/run/iacp-services/key":
                raise GateError(f"Falta la clave de servicios en {name}")
            if any(env.get(key) for key in ("IACP_PROXMOX_MNG_API_TOKEN", "IACP_PROXMOX_API_TOKEN")):
                raise GateError("Los workers de servicios no deben recibir credenciales Proxmox")
            if not _has_readonly_bind_mount(
                service_worker,
                source=str(service_key),
                target="/run/iacp-services/key",
            ):
                raise GateError("Los workers de servicios solo deben montar la clave Fernet de solo lectura")
            for mount in service_worker.get("volumes", []):
                if not isinstance(mount, dict) or mount.get("type") != "bind":
                    continue
                mount_source = str(mount.get("source", ""))
                if (
                    _bind_mount_overlaps(mount_source, service_directory)
                    and Path(mount_source).resolve(strict=False)
                    != service_key.resolve(strict=False)
                ):
                    raise GateError("Los workers de servicios no deben recibir la clave privada de la CA")
        if str(api_env.get("IACP_SERVICE_MAX_CONCURRENT", "2")) != str(len(service_worker_names)):
            raise GateError("La API debe anunciar la misma concurrencia del pool de servicios")
    if stage == "develop":
        _check_development_credentials(data, env_file, config)

    worker_envs = [_service_env(config, name) for name in worker_names]
    postgres_env = _service_env(config, "postgres")
    if config["services"]["postgres"].get("image") not in POSTGRES_ALLOWED_IMAGES:
        raise GateError("PostgreSQL debe usar la versión aprobada (postgres:17.6)")
    api_service = config["services"]["api"]
    api_networks = api_service.get("networks", {})
    proxy_aliases = api_networks.get("proxy", {}).get("aliases", []) if isinstance(api_networks, dict) else []
    postgres_networks = config["services"]["postgres"].get("networks", {})
    if (
        not isinstance(api_networks, dict)
        or not isinstance(postgres_networks, dict)
        or "internal" not in api_networks
        or "internal" not in postgres_networks
    ):
        raise GateError("La API y PostgreSQL deben compartir la red interna del stack")
    proxy_name = config.get("networks", {}).get("proxy", {}).get("name")
    if data["service_alias"] not in proxy_aliases or proxy_name != data["proxy_network"]:
        raise GateError("La API no tiene el alias esperado en la red compartida de Traefik")
    labels = api_service.get("labels", {})
    if isinstance(labels, list):
        labels = dict(item.split("=", 1) if "=" in item else (item, "") for item in labels)
    if any(
        str(key).startswith("traefik.http.routers.")
        or (key == "traefik.enable" and str(value).lower() == "true")
        for key, value in labels.items()
    ):
        raise GateError("El stack candidato no debe publicar rutas Traefik antes de la activación")
    if api_env.get("IACP_RUNTIME_STAGE") != expected_stage:
        raise GateError("IACP_RUNTIME_STAGE no coincide con el manifiesto")
    if not isinstance(data.get("wasabi_enabled"), bool):
        raise GateError("wasabi_enabled debe ser booleano en el manifiesto")
    expected_wasabi_flag = "true" if data["wasabi_enabled"] else "false"
    if api_env.get("IACP_WASABI_ENABLED", "false").strip().lower() != expected_wasabi_flag:
        raise GateError("IACP_WASABI_ENABLED no coincide con el manifiesto")
    if any(environment.get("IACP_WASABI_ENABLED", "false").strip().lower() != expected_wasabi_flag for environment in worker_envs):
        raise GateError("API y workers deben compartir IACP_WASABI_ENABLED")
    if api_env.get("IACP_DEFAULT_ENVIRONMENT") != "lab":
        raise GateError("El destino inicial debe ser lab/MNG")
    enabled = {part.strip() for part in str(api_env.get("IACP_ENABLED_ENVIRONMENTS", "")).split(",") if part.strip()}
    if enabled != {"lab"} or str(api_env.get("IACP_PRX_ENABLED", "0")) != "0":
        raise GateError("El stack debe exponer únicamente MNG y mantener PRX apagado")
    if str(api_env.get("IACP_MAX_CONCURRENT", "")) != "6" or data["max_concurrent"] != 6:
        raise GateError("API y manifiesto deben habilitar seis operaciones simultáneas")
    if any(str(environment.get("IACP_MAX_CONCURRENT", "")) != "6" for environment in worker_envs):
        raise GateError("Los seis workers deben compartir el límite de seis operaciones")
    additional_protected = str(api_env.get("IACP_ADDITIONAL_PROTECTED_VMIDS", ""))
    if any(
        str(environment.get("IACP_ADDITIONAL_PROTECTED_VMIDS", "")) != additional_protected
        for environment in worker_envs
    ):
        raise GateError("La lista de VMID protegidos debe coincidir entre API y los seis workers")
    expected_range = tuple(str(value) for value in expected_vmid_range)
    for service_environment in (api_env, *worker_envs):
        configured_range = (
            str(service_environment.get("IACP_VMID_MIN", "")),
            str(service_environment.get("IACP_VMID_MAX", "")),
        )
        if configured_range != expected_range:
            raise GateError(
                f"El rango VMID de API y workers debe ser "
                f"{expected_range[0]}–{expected_range[1]} en {stage}"
            )
    if str(api_env.get("IACP_ALLOW_INSECURE_LOCAL_DEV", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }:
        raise GateError("El stack no debe habilitar el modo local sin autenticación")
    try:
        root_password_minimum = int(api_env.get("IACP_ROOT_PASSWORD_MIN_LENGTH", "12"))
    except (TypeError, ValueError) as error:
        raise GateError("IACP_ROOT_PASSWORD_MIN_LENGTH debe ser un entero de al menos 12") from error
    if root_password_minimum < 12:
        raise GateError("IACP_ROOT_PASSWORD_MIN_LENGTH debe ser al menos 12")
    for key in ("IACP_API_TOKEN", "IACP_PROXMOX_MNG_API_TOKEN"):
        value = api_env.get(key)
        if not isinstance(value, str) or len(value.strip()) < 16 or "REEMPLAZAR" in value.upper():
            raise GateError(f"Falta configurar {key}")
    for environment in (api_env, *worker_envs):
        if (
            environment.get("IACP_PROXMOX_ENDPOINT") != environment.get("IACP_PROXMOX_MNG_ENDPOINT")
            or environment.get("IACP_PROXMOX_API_TOKEN") != environment.get("IACP_PROXMOX_MNG_API_TOKEN")
            or environment.get("IACP_TERRAFORM_DIR") != "environments/mng01"
        ):
            raise GateError("API y workers deben resolver el destino MNG y su Terraform canónico")
        endpoint = str(environment.get("IACP_PROXMOX_MNG_ENDPOINT", ""))
        try:
            parsed_endpoint = urlsplit(endpoint)
            endpoint_port = parsed_endpoint.port
        except ValueError as error:
            raise GateError("El endpoint Proxmox de MNG no es válido") from error
        if (
            parsed_endpoint.scheme != "https"
            or parsed_endpoint.hostname != "mng01.cyberfuel.com"
            or parsed_endpoint.username is not None
            or parsed_endpoint.password is not None
            or endpoint_port not in {None, 8006}
            or parsed_endpoint.path not in {"", "/", "/api2/json"}
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise GateError("El endpoint de MNG debe usar HTTPS hacia mng01.cyberfuel.com")
    database_password = postgres_env.get("POSTGRES_PASSWORD")
    if not isinstance(database_password, str) or len(database_password.strip()) < 16 or "REEMPLAZAR" in database_password.upper():
        raise GateError("Falta configurar POSTGRES_PASSWORD")
    if postgres_env.get("POSTGRES_DB") != expected_identity["database"]:
        raise GateError("La base PostgreSQL no coincide con el entorno")
    if postgres_env.get("POSTGRES_USER") != api_env.get("PGUSER"):
        raise GateError("El usuario PostgreSQL no coincide entre API y base")
    for environment in (api_env, *worker_envs, *[_service_env(config, name) for name in service_worker_names]):
        if (
            environment.get("PGHOST") != "postgres"
            or environment.get("PGPORT") != "5432"
            or environment.get("PGDATABASE") != expected_identity["database"]
            or environment.get("PGUSER") != postgres_env.get("POSTGRES_USER")
            or environment.get("PGPASSWORD") != database_password
        ):
            raise GateError("La conexión PostgreSQL no coincide entre los servicios del stack")
    if api_env.get("IACP_BUILD_REVISION") != data.get("image_tag"):
        raise GateError("La API no declara la revisión de imagen fijada")
    if any(api_env.get(key) for key in (
        "IACP_PROXMOX_PRX_ENDPOINT", "IACP_PROXMOX_PRX_API_TOKEN",
        "IACP_PROXMOX_PRX_POOL_ID", "IACP_PROXMOX_PRX_CREDENTIALS_FILE",
    )):
        raise GateError("PRX debe quedar sin configurar mientras esté deshabilitado")
    mng_host = "mng01.cyberfuel.com"
    host_mappings = []
    for service_name in ("api", *worker_names):
        mapping = _extra_host_map(config["services"][service_name]).get(mng_host)
        try:
            ipaddress.ip_address(str(mapping or ""))
        except ValueError as error:
            raise GateError(f"La IP de MNG no es válida en {service_name}") from error
        host_mappings.append(mapping)
    if len(set(host_mappings)) != 1:
        raise GateError("La resolución MNG debe coincidir en API y workers")
    expected_backend = "s3" if data["wasabi_enabled"] else "local"
    if data["backend"] != expected_backend:
        raise GateError("El backend declarado debe derivarse de wasabi_enabled")
    if data["wasabi_enabled"]:
        if stage != "production":
            raise GateError("Wasabi solo se habilita en el stack de producción")
        if data["state_bucket"] != "iacp-cyberfuel" or api_env.get("IACP_TFSTATE_BUCKET") != data["state_bucket"]:
            raise GateError("El bucket de producción no coincide con el aprobado")
        if data["state_prefix"].strip("/") != "iacp/prod-runtime-v1" or api_env.get("IACP_TFSTATE_PREFIX", "").strip("/") != data["state_prefix"].strip("/"):
            raise GateError("El prefijo de state no coincide con el aprobado")
        for worker_env in worker_envs:
            for key in ("IACP_WASABI_ACCESS_KEY_ID", "IACP_WASABI_SECRET_ACCESS_KEY"):
                value = worker_env.get(key)
                if not isinstance(value, str) or len(value.strip()) < 8 or "REEMPLAZAR" in value.upper():
                    raise GateError(f"Falta configurar la credencial de producción {key}")
        for key in ("IACP_WASABI_ACCESS_KEY_ID", "IACP_WASABI_SECRET_ACCESS_KEY"):
            if len({worker_env.get(key) for worker_env in worker_envs}) != 1:
                raise GateError("Las credenciales Wasabi deben coincidir entre los seis workers")
    else:
        if any(api_env.get(key) for key in ("IACP_TFSTATE_BUCKET", "IACP_TFSTATE_ENDPOINT", "IACP_TFSTATE_REGION", "IACP_TFSTATE_PREFIX")):
            raise GateError("Wasabi deshabilitado no debe configurar bucket, endpoint, región ni prefijo")
        if data["state_bucket"] or data["state_prefix"]:
            raise GateError("Con Wasabi deshabilitado, el manifiesto no debe declarar bucket ni prefijo")
        if any(worker_env.get("IACP_WASABI_ACCESS_KEY_ID") or worker_env.get("IACP_WASABI_SECRET_ACCESS_KEY") for worker_env in worker_envs):
            raise GateError("Wasabi deshabilitado no debe recibir credenciales Wasabi en los workers")
    repository = str(data.get("image_repository", "scm.cyberfuel.com/cyberfuel/iacp-controlplane"))
    if repository != "scm.cyberfuel.com/cyberfuel/iacp-controlplane":
        raise GateError("El repositorio de imagen no coincide con el artefacto aprobado")
    # El digest es opcional (equipo de desarrollo: se actualiza por commit). Si
    # el manifiesto lo declara, se exige coherencia con la imagen del Compose.
    image_digest = str(data.get("image_digest") or "")
    if image_digest and not IMAGE_DIGEST.fullmatch(image_digest):
        raise GateError("image_digest, si se declara, debe tener la forma sha256:<64 hex>")
    digest_suffix = f"@{image_digest}" if image_digest else ""
    image_tags = set()
    for service in app_names:
        image = config["services"][service].get("image", "")
        if not image.startswith(repository + ":"):
            raise GateError(f"La imagen de {service} no usa el repositorio aprobado")
        tag = image.split("@", 1)[0].rsplit(":", 1)[-1]
        if not COMMIT_TAG.fullmatch(tag):
            raise GateError(f"La imagen de {service} no está fijada a un commit inmutable")
        if image != f"{repository}:{tag}{digest_suffix}":
            raise GateError(f"La imagen de {service} no coincide con la imagen declarada en el manifiesto")
        image_tags.add(tag)
    if len(image_tags) != 1 or data.get("image_tag") not in image_tags:
        raise GateError("Los servicios y el manifiesto deben fijar la misma revisión de imagen")
    named_volumes = {
        volume.get("name", key)
        for key, volume in config.get("volumes", {}).items()
        if isinstance(volume, dict)
    }
    if named_volumes & LEGACY_VOLUMES:
        raise GateError("El stack nuevo intenta reutilizar un volumen legacy")
    if named_volumes != {expected_identity["postgres_volume"], expected_identity["workdata_volume"]}:
        raise GateError("Los volúmenes no son los exclusivos declarados para este entorno")
    stat = env_file.stat()
    if (stat.st_mode & 0o777) != 0o600:
        raise GateError("El archivo privado de entorno debe tener permisos 600")
    return {
        "stage": stage,
        "project": data["project"],
        "domain": data["domain"],
        "image_tag": next(iter(image_tags)),
        "volumes": sorted(named_volumes),
        "backend": data["backend"],
        "state_prefix": data["state_prefix"],
        "max_concurrent_per_stack": 6,
        "service_workers": list(service_worker_names),
        "service_max_concurrent": len(service_worker_names),
        "schema_version": schema_version,
        "vmid_range": {"min": expected_vmid_range[0], "max": expected_vmid_range[1]},
        "enabled_environments": ["lab"],
        "prx_enabled": False,
    }


def _check_wasabi_prefix(data: dict, config: dict) -> dict:
    if (
        data.get("stage") != "production"
        or data.get("wasabi_enabled") is not True
        or data.get("state_bucket") != "iacp-cyberfuel"
        or str(data.get("state_prefix", "")).strip("/") != "iacp/prod-runtime-v1"
    ):
        raise GateError("La comprobación Wasabi está limitada al bucket y prefijo productivos aprobados")
    api_env = _service_env(config, "api")
    worker_env = _service_env(config, "worker-1")
    endpoint = str(api_env.get("IACP_TFSTATE_ENDPOINT", ""))
    region = str(api_env.get("IACP_TFSTATE_REGION", ""))
    access_key = str(worker_env.get("IACP_WASABI_ACCESS_KEY_ID", ""))
    secret_key = str(worker_env.get("IACP_WASABI_SECRET_ACCESS_KEY", ""))
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "s3.us-east-1.wasabisys.com"
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or region != "us-east-1"
    ):
        raise GateError("El endpoint o la región Wasabi no coincide con la configuración aprobada")
    if not access_key or not secret_key or any(ch.isspace() for ch in access_key + secret_key):
        raise GateError("Las credenciales Wasabi no tienen un formato utilizable")

    bucket = "iacp-cyberfuel"
    prefix = "iacp/prod-runtime-v1/"
    host = parsed.netloc
    canonical_uri = "/" + quote(bucket, safe="-_.~")
    parameters = {"list-type": "2", "max-keys": "1", "prefix": prefix}
    canonical_query = "&".join(
        f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}"
        for key, value in sorted(parameters.items())
    )
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join((
        "GET", canonical_uri, canonical_query, canonical_headers,
        signed_headers, payload_hash,
    ))
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join((
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ))

    def sign(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode(), hashlib.sha256).digest()

    signing_key = sign(sign(sign(sign(("AWS4" + secret_key).encode(), date_stamp), region), "s3"), "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    url = urlunsplit((
        parsed.scheme, parsed.netloc, canonical_uri, canonical_query, "",
    ))
    request = Request(
        url,
        headers={
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": authorization,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read(65536)
            status = response.status
    except HTTPError as error:
        raise GateError(f"Wasabi rechazó la lectura del prefijo (HTTP {error.code})") from error
    except (OSError, URLError) as error:
        raise GateError(f"No se pudo consultar el prefijo Wasabi ({type(error).__name__})") from error
    if status != 200:
        raise GateError(f"Wasabi rechazó la lectura del prefijo (HTTP {status})")
    try:
        root = ET.fromstring(body)
        response_bucket = root.findtext("{*}Name")
        response_prefix = root.findtext("{*}Prefix")
    except ET.ParseError as error:
        raise GateError("Wasabi no devolvió una respuesta ListObjectsV2 válida") from error
    if response_bucket != bucket or response_prefix != prefix:
        raise GateError("Wasabi devolvió un bucket o prefijo distinto del aprobado")
    return {"bucket": bucket, "prefix": prefix.rstrip("/"), "list_access": "PASS", "writes_performed": False}


def _check_runtime_backend(data: dict, config: dict) -> dict | None:
    if data.get("wasabi_enabled") is True:
        return _check_wasabi_prefix(data, config)
    return {"backend": "local", "remote_state": "deshabilitado", "writes_performed": False}


def _new_evidence_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise GateError("La evidencia debe guardarse fuera del checkout")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(resolved, 0o700)
    return resolved


def _write_evidence(directory: Path, data: dict) -> None:
    path = directory / "result.json"
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _swap_upstream(route: Path, evidence: Path, expected: str, target: str) -> dict:
    if not route.is_file():
        raise GateError("No existe el archivo dinámico de Traefik")
    try:
        source = route.read_text(encoding="utf-8")
        parsed = yaml.safe_load(source) or {}
        current = parsed["http"]["services"]["iacp-api"]["loadBalancer"]["servers"][0]["url"]
    except (OSError, KeyError, IndexError, TypeError, yaml.YAMLError) as error:
        raise GateError("La ruta de Traefik tiene una forma inesperada") from error
    if current == target:
        return {"changed": False, "already_active": True, "target": target}
    if current != expected:
        raise GateError("El upstream activo cambió; se requiere revisión manual de Traefik")
    line_options = {
        f"url: {expected}": f"url: {target}",
        f'url: "{expected}"': f'url: "{target}"',
        f'"url": "{expected}"': f'"url": "{target}"',
        f"'url': '{expected}'": f"'url': '{target}'",
    }
    matches = [old_line for old_line in line_options if source.count(old_line) == 1]
    if len(matches) != 1:
        raise GateError("No se encontró una única línea del upstream esperado")
    old_line = matches[0]
    backup = evidence / "traefik-before.yml"
    if backup.exists():
        backup = evidence / "traefik-before-rollback.yml"
    shutil.copy2(route, backup)
    os.chmod(backup, 0o600)
    replacement = source.replace(old_line, line_options[old_line], 1)
    try:
        check = yaml.safe_load(replacement) or {}
        actual = check["http"]["services"]["iacp-api"]["loadBalancer"]["servers"][0]["url"]
    except (KeyError, IndexError, TypeError, yaml.YAMLError) as error:
        raise GateError("La configuración Traefik modificada no pasó validación") from error
    if actual != target:
        raise GateError("La configuración Traefik modificada no apunta al destino solicitado")
    temp = route.with_name(route.name + ".iacp-new")
    mode = route.stat().st_mode & 0o777
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(replacement)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, mode)
    os.replace(temp, route)
    directory_fd = os.open(route.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {"changed": True, "target": target}


def _dev_route_content(template: Path) -> str:
    try:
        content = template.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content) or {}
        if not isinstance(parsed, dict) or set(parsed) != {"http"}:
            raise TypeError("La plantilla debe contener solo HTTP dinámico")
        http = parsed["http"]
        if not isinstance(http, dict) or set(http) != {"routers", "services", "middlewares"}:
            raise TypeError("La sección HTTP de la plantilla no es cerrada")
        routers = http["routers"]
        router = routers["iacp-dev-admin"]
        http_router = routers["iacp-dev-http"]
        service = http["services"]["iacp-dev-api"]
        target = parsed["http"]["services"]["iacp-dev-api"]["loadBalancer"]["servers"][0]["url"]
        headers = http["middlewares"]["iacp-dev-headers"]["headers"]
    except (OSError, KeyError, IndexError, TypeError, yaml.YAMLError) as error:
        raise GateError("La plantilla Traefik de desarrollo no es válida") from error
    expected_rule = (
        "Host(`dev-iac.cyberfuel.com`) && (!HeaderRegexp(`Origin`, `.+`) || "
        "HeaderRegexp(`Origin`, `^(?:https://dev-iac\\.cyberfuel\\.com)$`))"
    )
    expected_middlewares = [
        "internal-sources@file", "strip-identity@file", "root-tls-transport@file",
        "request-rate@file", "inflight@file", "body-limit@file", "iacp-dev-headers",
    ]
    expected_headers = {
        "accessControlAllowOriginList": ["https://dev-iac.cyberfuel.com"],
        "accessControlAllowMethods": ["GET", "POST", "OPTIONS"],
        "accessControlAllowHeaders": ["Authorization", "Content-Type", "Idempotency-Key"],
        "accessControlExposeHeaders": ["X-Request-ID"],
        "addVaryHeader": True,
        "contentTypeNosniff": True,
        "stsSeconds": 31536000,
        "frameDeny": True,
        "referrerPolicy": "no-referrer",
    }
    if not all(isinstance(value, dict) for value in (routers, router, http_router, service, headers)):
        raise GateError("La estructura de la plantilla Traefik de desarrollo no es válida")
    if router.get("rule") != expected_rule:
        raise GateError("La ruta de desarrollo no corresponde al dominio aprobado")
    if (
        set(routers) != {"iacp-dev-admin", "iacp-dev-http"}
        or router.get("entryPoints") != ["websecure"]
        or router.get("service") != "iacp-dev-api"
        or router.get("tls") != {"options": "corporate"}
        or router.get("middlewares") != expected_middlewares
        or not isinstance(http_router, dict)
        or http_router.get("rule") != "Host(`dev-iac.cyberfuel.com`)"
        or http_router.get("entryPoints") != ["webhttp"]
        or http_router.get("service") != "noop@internal"
        or http_router.get("middlewares") != ["internal-sources@file", "https-only@file"]
    ):
        raise GateError("La ruta de desarrollo no conserva las restricciones de acceso aprobadas")
    if (
        set(http["services"]) != {"iacp-dev-api"}
        or set(http["middlewares"]) != {"iacp-dev-headers"}
        or not isinstance(service, dict)
        or service != {"loadBalancer": {"servers": [{"url": "http://iacp-controlplane-api-dev:8080"}]}}
        or target != "http://iacp-controlplane-api-dev:8080"
        or headers != expected_headers
    ):
        raise GateError("La ruta de desarrollo no apunta al alias aprobado")
    return content


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _install_dev_route(route: Path, production_route: Path, template: Path, evidence: Path) -> dict:
    content = _dev_route_content(template)
    if route.parent.is_symlink() or route.parent.resolve() != route.parent or not route.parent.is_dir():
        raise GateError("El directorio dinámico de Traefik no existe o usa un enlace simbólico")
    if route.is_symlink():
        raise GateError("La ruta de desarrollo no puede ser un enlace simbólico")
    if route.exists():
        if route.stat().st_mode & 0o022:
            raise GateError("Los permisos del archivo de desarrollo no son seguros")
        try:
            current = route.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise GateError("No se pudo leer la ruta existente de desarrollo") from error
        if current != content:
            raise GateError("La ruta de desarrollo ya existe con contenido distinto; requiere revisión")
        return {"changed": False, "already_active": True, "target": "http://iacp-controlplane-api-dev:8080"}
    if production_route.is_symlink() or not production_route.is_file():
        raise GateError("No está presente la ruta productiva que confirma el directorio dinámico de Traefik")
    try:
        current_production = yaml.safe_load(production_route.read_text(encoding="utf-8")) or {}
        current_upstream = current_production["http"]["services"]["iacp-api"]["loadBalancer"]["servers"][0]["url"]
    except (OSError, KeyError, IndexError, TypeError, yaml.YAMLError) as error:
        raise GateError("La ruta productiva de Traefik tiene una forma inesperada") from error
    if not isinstance(current_upstream, str) or not current_upstream.startswith("http://"):
        raise GateError("La ruta productiva de Traefik no tiene un upstream HTTP válido")
    if not route.parent.is_dir():
        raise GateError("No existe el directorio dinámico de Traefik")
    directory_mode = route.parent.stat().st_mode & 0o777
    if directory_mode & 0o022:
        raise GateError("Los permisos del directorio dinámico no son seguros")
    mode = production_route.stat().st_mode & 0o777
    temp_fd, temp_name = tempfile.mkstemp(prefix=".iacp-dev-route-", dir=route.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, mode)
        evidence_copy = evidence / "dev-route-installed.yml"
        evidence_copy.write_text(content, encoding="utf-8")
        os.chmod(evidence_copy, 0o600)
        try:
            os.link(temp_path, route)
        except FileExistsError as error:
            try:
                current = route.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise GateError("La ruta de desarrollo apareció durante la activación") from error
            if current != content:
                raise GateError("La ruta de desarrollo cambió durante la activación") from error
            return {"changed": False, "already_active": True, "target": "http://iacp-controlplane-api-dev:8080"}
        _fsync_directory(route.parent)
    finally:
        temp_path.unlink(missing_ok=True)
    return {"changed": True, "target": "http://iacp-controlplane-api-dev:8080"}


def _remove_dev_route(route: Path, template: Path, evidence: Path) -> dict:
    content = _dev_route_content(template)
    if route.is_symlink() or not route.is_file():
        raise GateError("No existe una ruta administrada de desarrollo que se pueda retirar")
    try:
        current = route.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise GateError("No se pudo leer la ruta de desarrollo") from error
    if current != content:
        raise GateError("La ruta de desarrollo fue modificada; no se retira automáticamente")
    backup = evidence / "dev-route-before-removal.yml"
    shutil.copy2(route, backup)
    os.chmod(backup, 0o600)
    route.unlink()
    _fsync_directory(route.parent)
    return {"changed": True, "removed": str(route)}


def _dns_pattern_matches(pattern: str, hostname: str) -> bool:
    if pattern == hostname:
        return True
    if not pattern.startswith("*."):
        return False
    pattern_labels = pattern[2:].split(".")
    hostname_labels = hostname.split(".")
    return len(hostname_labels) == len(pattern_labels) + 1 and hostname_labels[1:] == pattern_labels


def _require_tls_hostname(certificate_path: Path, hostname: str) -> None:
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", str(certificate_path), "-noout", "-ext", "subjectAltName"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateError("No se pudo leer el certificado TLS configurado") from error
    if result.returncode:
        raise GateError("No se pudo leer el certificado TLS configurado")
    dns_names = re.findall(r"\bDNS:([^,\s]+)", result.stdout)
    if not any(_dns_pattern_matches(pattern, hostname) for pattern in dns_names):
        raise GateError(f"El certificado TLS actual no cubre {hostname}")


def _inventory(data: dict, compose: Path, env_file: Path) -> dict:
    prefix = _compose_args(data, compose, env_file)
    raw = _run(prefix + ["ps", "--format", "json"], label="Inventario Compose")
    containers = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append({
            "service": entry.get("Service"),
            "state": entry.get("State"),
            "health": entry.get("Health"),
            "image": entry.get("Image"),
        })
    return {"project": data["project"], "container_count": len(containers), "containers": containers}


def _verify(data: dict, compose: Path, env_file: Path) -> dict:
    prefix = _compose_args(data, compose, env_file)
    statuses = _inventory(data, compose, env_file)["containers"]
    expected = {"postgres", "api", *(f"worker-{number}" for number in range(1, 7))}
    if data.get("services_enabled", False):
        expected.update(_service_worker_names(data))
    if {item["service"] for item in statuses} != expected:
        raise GateError("No están activos PostgreSQL, API y los seis workers esperados")
    if any(item["state"] != "running" for item in statuses):
        raise GateError("Hay servicios del stack que no están running")
    postgres = next(item for item in statuses if item["service"] == "postgres")
    api = next(item for item in statuses if item["service"] == "api")
    if postgres["health"] != "healthy" or api["health"] != "healthy":
        raise GateError("PostgreSQL o la API no está healthy")
    # Los workers declaran healthcheck por heartbeat; «starting» se tolera
    # durante su start_period, pero un worker unhealthy bloquea el gate.
    unhealthy = sorted(
        item["service"] for item in statuses
        if str(item["service"]).startswith(("worker-", "service-worker-")) and item["health"] == "unhealthy"
    )
    if unhealthy:
        raise GateError(f"Workers unhealthy: {', '.join(unhealthy)}")
    script = """
import json, os, time, urllib.request
from datetime import datetime, timezone
from api.db import database
health_response = urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=5)
health = json.load(health_response)
revision = health_response.headers.get('X-IACP-Build-Revision', '')
spec = json.load(urllib.request.urlopen('http://127.0.0.1:8080/openapi.json', timeout=5))
expected_workers = [f'worker-{number}' for number in range(1, 7)]
deadline = time.monotonic() + 60
worker_ids = []
schema_version = None
core_tables = set()
while time.monotonic() < deadline:
    with database() as db:
        row = db.execute('SELECT MAX(version) AS version FROM schema_migrations').fetchone()
        schema_version = row['version'] if row else None
        worker_ids = [row['worker_id'] for row in db.execute('SELECT worker_id FROM workers ORDER BY worker_id').fetchall()]
        core_tables = {row['table_name'] for row in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()}
    if set(worker_ids) >= set(expected_workers):
        break
    time.sleep(1)
def worker_is_fresh(row):
    if row['status'] not in {'idle', 'running'} or not row['last_heartbeat']:
        return False
    try:
        heartbeat_at = datetime.fromisoformat(row['last_heartbeat'])
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - heartbeat_at).total_seconds()
        return 0 <= age < 60
    except (TypeError, ValueError):
        return False
worker_deadline = time.monotonic() + 60
fresh_worker_ids = []
while time.monotonic() < worker_deadline:
    with database() as db:
        rows = db.execute('SELECT worker_id, status, last_heartbeat FROM workers ORDER BY worker_id').fetchall()
        fresh_worker_ids = [row['worker_id'] for row in rows if worker_is_fresh(row)]
    if fresh_worker_ids == expected_workers:
        break
    time.sleep(1)
print(json.dumps({
    'health': health.get('status'),
    'api_version': spec.get('info', {}).get('version'),
    'provision_path': '/v1/instances' in spec.get('paths', {}),
    'build_revision': revision,
    'schema_version': schema_version,
    'ip_allocations_table': 'ip_allocations' in core_tables,
    'worker_ids': worker_ids,
    'infrastructure_workers_fresh': fresh_worker_ids == expected_workers,
}))
"""
    expected_service_workers = list(_service_worker_names(data))
    if data.get("services_enabled", False):
        script = script.replace("print(json.dumps({", "payload = {").replace("\n}))", "\n}")
        script += """
service_worker_ids = []
service_deadline = time.monotonic()+60
while time.monotonic()<service_deadline:
    with database() as db:
        rows = db.execute('SELECT worker_id, status, last_heartbeat FROM service_workers ORDER BY worker_id').fetchall()
        service_worker_ids = [row['worker_id'] for row in rows if row['status'] in {'idle','running'} and
            0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(row['last_heartbeat'])).total_seconds() < 60]
    if service_worker_ids == EXPECTED_SERVICE_WORKERS:
        break
    time.sleep(1)
from api.service_secrets import cipher
cipher()
payload['service_key_ready'] = True
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key
ca_cert_path = Path(os.environ.get('IACP_SERVICE_CA_CERT_FILE', ''))
ca_key_path = Path(os.environ.get('IACP_SERVICE_CA_KEY_FILE', ''))
ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
ca_key = load_pem_private_key(ca_key_path.read_bytes(),password=None)
assert ca_cert.public_key().public_numbers()==ca_key.public_key().public_numbers()
payload['service_ca_cert_ready'] = True
payload['service_api'] = all(path in spec.get('paths', {}) for path in ['/v1/applications','/v1/service-instances','/v1/service-workers'])
payload['service_worker_ids'] = service_worker_ids
payload['service_tables'] = all(name in core_tables for name in ['service_instances','service_jobs','service_events','service_workers','service_releases','service_bindings'])
service_response = urllib.request.urlopen(urllib.request.Request(
    'http://127.0.0.1:8080/v1/service-workers',
    headers={'Authorization': 'Bearer ' + os.environ['IACP_API_TOKEN']},
    ), timeout=5)
service_capacity = json.load(service_response)
payload['service_max_concurrent'] = service_capacity.get('max_concurrent')
print(json.dumps(payload))
"""
        script = script.replace(
            "EXPECTED_SERVICE_WORKERS", repr(expected_service_workers)
        )
    result = _run(prefix + ["exec", "-T", "api", "python", "-c", script], label="Verificación API")
    try:
        payload = json.loads(result.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as error:
        raise GateError("La verificación API no devolvió JSON válido") from error
    expected_revision = str(data.get("image_tag", ""))
    expected_payload = {
        "health": "ready",
        "api_version": "1.0.0",
        "provision_path": True,
        "build_revision": expected_revision,
        "schema_version": data.get("schema_version", 2),
        "ip_allocations_table": True,
        "worker_ids": [f"worker-{number}" for number in range(1, 7)],
        "infrastructure_workers_fresh": True,
    }
    if data.get("services_enabled", False):
        expected_payload.update(service_worker_ids=expected_service_workers, service_max_concurrent=len(expected_service_workers), service_tables=True, service_key_ready=True, service_ca_cert_ready=True, service_api=True, infrastructure_workers_fresh=True)
    if payload != expected_payload:
        raise GateError("API, PostgreSQL, migración y pools de workers no cumplen el contrato declarado")
    if data.get("services_enabled", False):
        key_check = (
            "from api.service_secrets import cipher; cipher(); "
            "print('IACP_SERVICE_KEY_READY')"
        )
        for worker_name in (f"worker-{number}" for number in range(1, 7)):
            output = _run(
                prefix + ["exec", "-T", worker_name, "python", "-c", key_check],
                label=f"Verificación de clave de servicios en {worker_name}",
            )
            if output.strip().splitlines()[-1:] != ["IACP_SERVICE_KEY_READY"]:
                raise GateError(f"{worker_name} no puede leer la clave de servicios")
    return {"services": statuses, "api": payload}


BACKUP_FORMAT = 1
BACKUP_FILES = ("postgres.dump", "workdata.tar.gz")
WORKDATA_PATH = "/var/lib/iacp-controlplane"
POSTGRES_IMAGE = "postgres:17.6@sha256:00bc86618629af00d2937fdc5a5d63db3ff8450acf52f0636ec813c7f4902929"
RESTORE_IMAGE = POSTGRES_IMAGE
POSTGRES_ALLOWED_IMAGES = {POSTGRES_IMAGE, POSTGRES_IMAGE.split("@", 1)[0]}
_COUNT_SQL = (
    "SELECT json_build_object("
    "'schema_version', (SELECT MAX(version) FROM schema_migrations),"
    "'instances', (SELECT COUNT(*) FROM instances),"
    "'operations', (SELECT COUNT(*) FROM operations),"
    "'active_operations', (SELECT COUNT(*) FROM operations WHERE status IN "
    "('running','planning','applying','destroying','powering','uncertain')) ,"
    "'active_service_jobs', (SELECT COUNT(*) FROM service_jobs WHERE status IN ('running','uncertain')) ,"
    "'running_operations', (SELECT COUNT(*) FROM operations WHERE status IN "
    "('running','planning','applying','destroying','powering')) ,"
    "'running_service_jobs', (SELECT COUNT(*) FROM service_jobs WHERE status='running') ,"
    "'uncertain_operations', (SELECT COUNT(*) FROM operations WHERE status='uncertain') ,"
    "'uncertain_service_jobs', (SELECT COUNT(*) FROM service_jobs WHERE status='uncertain') ,"
    "'queued_operations', (SELECT COUNT(*) FROM operations WHERE status IN ('pending','queued')) ,"
    "'queued_service_jobs', (SELECT COUNT(*) FROM service_jobs WHERE status='queued') ,"
    "'runnable_queued_service_jobs', (SELECT COUNT(*) FROM service_jobs j "
    "JOIN service_instances s ON s.service_id=j.service_id "
    "JOIN instances i ON i.instance_id=s.instance_id "
    "WHERE j.status='queued' AND i.status='ready' AND i.power_state='running' "
    "AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.instance_id=i.instance_id "
    "AND o.status IN ('pending','running','planning','applying','destroying','powering','uncertain'))) ,"
    "'busy_infrastructure_workers', (SELECT COUNT(*) FROM workers WHERE status <> 'idle' OR current_operation_id IS NOT NULL) ,"
    "'busy_service_workers', (SELECT COUNT(*) FROM service_workers WHERE status <> 'idle' OR job_id IS NOT NULL) ,"
    "'vmid_leases_held', (SELECT COUNT(*) FROM vmid_leases WHERE status='held'))"
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_to_file(command: list[str], path: Path, *, label: str, timeout: int = 1800) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            result = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, timeout=timeout)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if result.returncode or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise GateError(f"{label} terminó con código {result.returncode}")


def _database_counts(prefix: list[str]) -> dict:
    raw = _run(
        prefix + [
            "exec", "-T", "postgres", "sh", "-c",
            'psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$0"', _COUNT_SQL,
        ],
        label="Conteo de PostgreSQL",
    )
    return json.loads(raw.strip().splitlines()[-1])


@contextmanager
def _runtime_write_gate(prefix: list[str]):
    """Block new API mutations and worker claims for a coordinated operation."""

    command = prefix + [
        "exec", "-T", "postgres", "sh", "-c",
        'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + 30
    acquired = False
    try:
        process.stdin.write(
            b"SELECT pg_advisory_lock(104, 103);\n"
            b"SELECT 'IACP_RUNTIME_WRITE_GATE_HELD';\n"
        )
        process.stdin.flush()
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            events = selector.select(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            output.extend(chunk)
            if b"IACP_RUNTIME_WRITE_GATE_HELD" in output:
                acquired = True
                break
        if not acquired:
            detail = bytes(output).decode("utf-8", errors="replace")[-500:]
            raise GateError(
                "No se pudo activar la barrera transaccional de escritura"
                + (f": {detail.strip()}" if detail.strip() else "")
            )
        yield
    finally:
        selector.close()
        if process.poll() is None:
            try:
                process.stdin.write(
                    b"SELECT pg_advisory_unlock(104, 103);\n\\q\n"
                )
                process.stdin.flush()
                process.stdin.close()
                process.wait(timeout=10)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        elif process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


def _wait_for_stack_quiescence(
    prefix: list[str], *, timeout: float = 2100, allow_uncertain: bool = False
) -> dict:
    """Espera trabajo ejecutable y workers ocupados antes del despliegue.

    Una operación ``uncertain`` no la ejecuta ningún worker: es una intención a
    la espera de reconciliación. Por defecto bloquea el drenaje, pero eso crea un
    interbloqueo cuando la corrección que debe reconciliarla va en el propio
    despliegue. Con ``allow_uncertain`` solo se espera el trabajo en ejecución y
    las intenciones inciertas se conservan intactas y quedan registradas.

    Un job de servicio ``queued`` solo bloquea cuando satisface el predicado de
    ``worker.services.claim``: infraestructura lista, VM encendida y sin una
    operación de infraestructura activa. Los demás permanecen en la base, se
    devuelven como diferidos y no pueden ser reclamados durante el despliegue.
    """

    operation_key, job_key = (
        ("running_operations", "running_service_jobs")
        if allow_uncertain
        else ("active_operations", "active_service_jobs")
    )
    deadline = time.monotonic() + timeout
    while True:
        counts = _database_counts(prefix)
        if not any(
            int(counts.get(key) or 0)
            for key in (
                operation_key,
                job_key,
                "queued_operations",
                "runnable_queued_service_jobs",
                "busy_infrastructure_workers",
                "busy_service_workers",
            )
        ):
            if allow_uncertain:
                counts = dict(counts, uncertain_deferred=True)
            deferred_service_jobs = max(
                0,
                int(counts.get("queued_service_jobs") or 0)
                - int(counts.get("runnable_queued_service_jobs") or 0),
            )
            if deferred_service_jobs:
                counts = dict(
                    counts,
                    non_runnable_queued_service_jobs_deferred=deferred_service_jobs,
                )
            return counts
        if time.monotonic() >= deadline:
            raise GateError("El stack no terminó de drenar operaciones y workers activos")
        time.sleep(5)


def _backup(
    data: dict, compose: Path, env_file: Path, destination: Path, *, allow_uncertain: bool = False
) -> dict:
    """Respaldo para recuperación ante desastres de un stack V1.

    El volcado de PostgreSQL es una instantánea consistente (pg_dump). El
    archivo de workdata (workspaces y states de Terraform) es consistente
    ante caídas; por eso se registran las operaciones activas al momento del
    respaldo. Ambos contienen datos sensibles (los states incluyen la
    contraseña root marcada como sensitive): permisos 600 y directorio 700.
    """

    resolved = destination.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise GateError("El respaldo debe guardarse fuera del checkout")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(resolved, 0o700)
    prefix = _compose_args(data, compose, env_file)
    started = time.monotonic()
    with _runtime_write_gate(prefix):
        counts = _wait_for_stack_quiescence(prefix, allow_uncertain=allow_uncertain)
        _stream_to_file(
            prefix + [
                "exec", "-T", "postgres", "sh", "-c",
                'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges',
            ],
            resolved / "postgres.dump",
            label="pg_dump",
        )
        _stream_to_file(
            prefix + ["exec", "-T", "api", "tar", "-C", WORKDATA_PATH, "-czf", "-", "."],
            resolved / "workdata.tar.gz",
            label="Archivo de workdata",
        )
    manifest = {
        "format": BACKUP_FORMAT,
        "project": data["project"],
        "stage": data.get("stage"),
        "domain": data.get("domain"),
        "image_tag": data.get("image_tag"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": counts,
        "workdata_coordinated_with_database": True,
        "files": {
            name: {"sha256": _sha256_file(resolved / name), "bytes": (resolved / name).stat().st_size}
            for name in BACKUP_FILES
        },
        "seconds": round(time.monotonic() - started, 3),
    }
    manifest_path = resolved / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return {"backup_dir": str(resolved), **manifest}


def _backup_verify(backup_dir: Path) -> dict:
    """Prueba de restauración: integridad, tar legible y pg_restore real.

    Restaura en un PostgreSQL efímero sin red y en tmpfs; no toca los stacks.
    """

    resolved = backup_dir.resolve()
    manifest = json.loads((resolved / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != BACKUP_FORMAT:
        raise GateError("Formato de respaldo no soportado")
    for name in BACKUP_FILES:
        expected = manifest["files"][name]["sha256"]
        if not hmac.compare_digest(_sha256_file(resolved / name), expected):
            raise GateError(f"El SHA-256 de {name} no coincide con el manifiesto")
    listing = _run(["tar", "-tzf", str(resolved / "workdata.tar.gz")], label="Lectura de workdata", timeout=900)
    entries = [line for line in listing.splitlines() if line.strip()]
    tfstates = [line for line in entries if line.endswith("terraform.tfstate")]
    container = f"iacp-restore-check-{os.getpid()}-{int(time.time())}"
    password = hashlib.sha256(os.urandom(32)).hexdigest()
    _run([
        "docker", "run", "-d", "--rm", "--name", container, "--network", "none",
        "--tmpfs", "/var/lib/postgresql/data", "-e", f"POSTGRES_PASSWORD={password}",
        "-e", "POSTGRES_DB=restore_check", RESTORE_IMAGE,
    ], label="PostgreSQL efímero de restauración")
    try:
        deadline = time.monotonic() + 90
        while True:
            ready = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", "postgres", "-d", "restore_check"],
                capture_output=True, timeout=10,
            )
            if ready.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise GateError("El PostgreSQL efímero no quedó listo")
            time.sleep(1)
        with (resolved / "postgres.dump").open("rb") as dump:
            restore = subprocess.run(
                ["docker", "exec", "-i", container, "pg_restore", "-U", "postgres", "-d", "restore_check",
                 "--no-owner", "--no-privileges", "--exit-on-error"],
                stdin=dump, capture_output=True, timeout=1800,
            )
        if restore.returncode:
            raise GateError(f"pg_restore terminó con código {restore.returncode}")
        raw = _run(
            ["docker", "exec", container, "psql", "-X", "-At", "-U", "postgres", "-d", "restore_check", "-c", _COUNT_SQL],
            label="Conteo restaurado",
        )
        restored = json.loads(raw.strip().splitlines()[-1])
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)
    if restored.get("schema_version") != manifest["database"].get("schema_version"):
        raise GateError("La versión de esquema restaurada no coincide con la respaldada")
    for key in ("instances", "operations"):
        if int(restored.get(key) or 0) < int(manifest["database"].get(key) or 0):
            raise GateError(f"La restauración tiene menos filas de {key} que el respaldo")
    return {
        "backup_dir": str(resolved),
        "project": manifest.get("project"),
        "created_at": manifest.get("created_at"),
        "checksums": "ok",
        "workdata_entries": len(entries),
        "terraform_states": len(tfstates),
        "restored": restored,
        "recorded": manifest["database"],
    }


def _verify_preloaded_images(config: dict) -> list[dict[str, str]]:
    services = config.get("services", {})
    verified = []
    expected_application_image_id = None
    for service_name in sorted(services):
        image = services[service_name].get("image")
        if not isinstance(image, str) or not image.strip():
            raise GateError(f"El servicio {service_name} no declara una imagen para despliegue precargado")
        image_id = _run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            label=f"Verificación de imagen precargada {service_name}",
        ).strip()
        if not image_id.startswith("sha256:"):
            raise GateError(f"La imagen precargada de {service_name} no devolvió un digest local válido")
        item = {"service": service_name, "image": image, "image_id": image_id}
        match = re.fullmatch(
            r"(?P<repository>[^@]+):(?P<tag>[^@]+)(?:@(?P<digest>sha256:[0-9a-f]{64}))?",
            image,
        )
        if match is None:
            raise GateError(f"La imagen de {service_name} no tiene un formato repositorio:tag[@digest] válido")
        if service_name == "postgres" and image not in POSTGRES_ALLOWED_IMAGES:
            raise GateError("PostgreSQL no usa la versión aprobada por el operador")
        if service_name != "postgres" and not re.fullmatch(r"[0-9a-f]{40}", match["tag"]):
            raise GateError(f"La imagen de {service_name} no está fijada por commit")
        if match["digest"]:
            # Con digest declarado, el contenido local debe coincidir con él.
            repo_digests = _run(
                ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image],
                label=f"Digest de registro de {service_name}",
            ).strip()
            try:
                recorded_digests = json.loads(repo_digests)
            except json.JSONDecodeError as error:
                raise GateError(f"Docker no devolvió RepoDigests válidos para {service_name}") from error
            expected_repo_digest = f"{match['repository']}@{match['digest']}"
            if not isinstance(recorded_digests, list) or expected_repo_digest not in recorded_digests:
                raise GateError(f"El contenido local de {service_name} no coincide con el digest declarado")
        if service_name != "postgres":
            revision = _run(
                [
                    "docker", "image", "inspect", "--format",
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}', image,
                ],
                label=f"Revisión de imagen de {service_name}",
            ).strip()
            if revision != match["tag"]:
                raise GateError(f"La etiqueta de revisión de {service_name} no coincide con el commit aprobado")
            if expected_application_image_id is None:
                expected_application_image_id = image_id
            elif image_id != expected_application_image_id:
                raise GateError("API y workers no usan los mismos bytes de imagen")
            item["registry_digest"] = match["digest"] or ""
        else:
            item["registry_digest"] = match["digest"] or ""
        verified.append(item)
    if not verified:
        raise GateError("La configuración no contiene imágenes para el despliegue precargado")
    return verified


def _active_upstream(route: Path) -> str:
    if not route.is_file():
        raise GateError("No existe el archivo dinámico de Traefik")
    try:
        parsed = yaml.safe_load(route.read_text(encoding="utf-8")) or {}
        return parsed["http"]["services"]["iacp-api"]["loadBalancer"]["servers"][0]["url"]
    except (OSError, KeyError, IndexError, TypeError, yaml.YAMLError) as error:
        raise GateError("La ruta de Traefik tiene una forma inesperada") from error


def _assert_production_candidate_inactive(data: dict) -> None:
    if data.get("stage") != "production":
        return
    route = Path(data["dynamic_config"])
    candidate = f"http://{data['service_alias']}:8080"
    if _active_upstream(route) == candidate:
        raise GateError(
            "El alias candidato está activo; no se puede reemplazar antes de verificar otra revisión"
        )


def _activate_production(data: dict, evidence: Path) -> dict:
    if data["stage"] != "production":
        raise GateError("La activación productiva requiere el manifiesto production")
    if os.geteuid() != 0:
        raise GateError("La activación productiva requiere ejecutar el operador como root")
    certification_path = Path(data.get("certification_file", ""))
    if certification_path != Path("/etc/iacp/certification/controlplane-v1.json"):
        raise GateError("La certificación debe proceder del directorio protegido de evidencias")
    if not certification_path.is_file():
        raise GateError("Falta la certificación física PASS ligada a esta imagen")
    cert_stat = certification_path.stat()
    cert_directory_stat = certification_path.parent.stat()
    if cert_stat.st_mode & 0o077 or cert_directory_stat.st_mode & 0o077:
        raise GateError("La certificación y su directorio deben ser privados para el equipo de operaciones")
    try:
        certificate = json.loads(certification_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError("La certificación no se puede leer") from error
    image_tag = str(data.get("image_tag", ""))
    required = {
        "unit_tests", "api_contract", "physical_provision", "physical_delete",
        "reconciliation", "state_lock", "protected_vmids", "mng_isolation",
    }
    results = certificate.get("checks", {}) if isinstance(certificate, dict) else {}
    if certificate.get("result") != "PASS" or certificate.get("image_tag") != image_tag:
        raise GateError("La certificación no corresponde al release que se va a activar")
    if data.get("image_digest") and certificate.get("image_digest") != data.get("image_digest"):
        raise GateError("La certificación no corresponde al digest de imagen que se va a activar")
    if not required.issubset(results) or any(results.get(key) != "PASS" for key in required):
        raise GateError("La certificación no cubre todos los gates productivos")
    route = Path(data["dynamic_config"]).resolve()
    if route != Path("/etc/iacp/tls/dynamic/iacp.yml"):
        raise GateError("La activación productiva está limitada a la ruta Traefik autorizada")
    target = f"http://{data['service_alias']}:8080"
    if _active_upstream(route) != target:
        _require_legacy_api_healthy()
    update = _swap_upstream(route, evidence, "http://api:8000", target)
    return {
        "activated": update["changed"],
        "already_active": update.get("already_active", False),
        "target": target,
    }


def _restore_legacy(data: dict, evidence: Path) -> dict:
    if data.get("stage") != "production":
        raise GateError("La restauración legacy solo aplica al manifiesto production")
    if data.get("domain") != "iac.cyberfuel.com" or data.get("service_alias") != "iacp-controlplane-api-prod":
        raise GateError("El manifiesto no identifica la ruta productiva aprobada")
    if Path(data["dynamic_config"]).resolve() != Path("/etc/iacp/tls/dynamic/iacp.yml"):
        raise GateError("La restauración está limitada a la ruta productiva conocida")
    if os.geteuid() != 0:
        raise GateError("La restauración requiere ejecutar el operador como root")
    _require_legacy_api_healthy()
    route = Path(data["dynamic_config"]).resolve()
    candidate = f"http://{data['service_alias']}:8080"
    update = _swap_upstream(route, evidence, candidate, "http://api:8000")
    return {
        "restored": update["changed"],
        "already_legacy": update.get("already_active", False),
        "target": "http://api:8000",
        "legacy_api_healthy": True,
        "automatic_rollback": False,
    }


def _rollback_failed_activation(data: dict, evidence: Path) -> dict:
    """Restore the legacy upstream if an earlier attempt left the candidate active."""

    route = Path(data["dynamic_config"]).resolve()
    candidate = f"http://{data['service_alias']}:8080"
    legacy = "http://api:8000"
    active = _active_upstream(route)
    if active == legacy:
        _require_legacy_api_healthy()
        return {"restored": False, "already_legacy": True, "target": legacy}
    if active != candidate:
        raise GateError("El upstream activo cambió a un destino no reconocido; rollback automático retenido")
    _require_legacy_api_healthy()
    update = _swap_upstream(route, evidence, candidate, legacy)
    return {
        "restored": bool(update.get("changed")),
        "already_legacy": bool(update.get("already_active")),
        "target": legacy,
    }


def _develop_route_paths(data: dict) -> tuple[Path, Path, Path, Path]:
    if (
        data.get("stage") != "develop"
        or data.get("domain") != "dev-iac.cyberfuel.com"
        or data.get("service_alias") != "iacp-controlplane-api-dev"
    ):
        raise GateError("El manifiesto no identifica el runtime de desarrollo aprobado")
    route = Path(data.get("dynamic_config", ""))
    if route != Path("/etc/iacp/tls/dynamic/dev-iac.yml"):
        raise GateError("La ruta de desarrollo está limitada al archivo dinámico conocido")
    template = (ROOT / str(data.get("route_template", ""))).resolve()
    if ROOT not in template.parents or template != ROOT / "deploy/portainer/traefik-dev.yml":
        raise GateError("La plantilla Traefik no es la aprobada")
    certificate = Path(data.get("tls_certificate_file", ""))
    if certificate != Path("/etc/iacp/tls/certs/fullchain.pem"):
        raise GateError("La activación está limitada al certificado TLS administrado conocido")
    return route, Path("/etc/iacp/tls/dynamic/iacp.yml"), template, certificate


def _activate_develop(data: dict, evidence: Path) -> dict:
    route, production_route, template, certificate = _develop_route_paths(data)
    if os.geteuid() != 0:
        raise GateError("La activación de desarrollo requiere ejecutar el operador como root")
    _require_tls_hostname(certificate, data["domain"])
    update = _install_dev_route(route, production_route, template, evidence)
    try:
        external = _check_external(data["domain"], str(data.get("image_tag", "")))
    except GateError as error:
        if update["changed"]:
            _remove_dev_route(route, template, evidence)
            raise GateError(f"{error}; la ruta recién creada se retiró automáticamente") from error
        raise
    return {
        "activated": update["changed"],
        "already_active": update.get("already_active", False),
        "target": update["target"],
        "external_health": external,
        "automatic_rollback": bool(update["changed"] and not external.get("ready")),
    }


def _disable_develop(data: dict, evidence: Path) -> dict:
    route, _, template, _ = _develop_route_paths(data)
    if os.geteuid() != 0:
        raise GateError("Retirar la ruta de desarrollo requiere ejecutar el operador como root")
    result = _remove_dev_route(route, template, evidence)
    return {"disabled": result["changed"], "route": result["removed"]}


def _require_legacy_api_healthy() -> None:
    raw = _run([
        "docker", "ps",
        "--filter", "label=com.docker.compose.project=iacp-controlplane",
        "--filter", "label=com.docker.compose.service=api",
        "--format", "json",
    ], label="Comprobación de API legacy")
    containers = []
    for line in raw.splitlines():
        if line.strip():
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise GateError("Docker no devolvió un inventario válido del API legacy") from error
    if len(containers) != 1 or containers[0].get("State") != "running" or containers[0].get("HealthStatus") != "healthy":
        raise GateError("La API legacy no está única, running y healthy; no se cambia la ruta")


def _check_external(
    domain: str,
    image_tag: str | None,
    *,
    forbidden_revision: str | None = None,
    retries: int = 5,
    retry_delay: float = 2,
) -> dict:
    url = f"https://{domain}/health/ready"
    last_error = "El dominio no devolvió readiness"
    for attempt in range(max(0, retries) + 1):
        try:
            with urlopen(url, timeout=15) as response:
                payload = json.loads(response.read())
                status = response.status
                revision = response.headers.get("X-IACP-Build-Revision", "")
        except (OSError, URLError, json.JSONDecodeError) as error:
            last_error = f"La comprobación externa falló ({type(error).__name__})"
        else:
            if status == 200 and isinstance(payload, dict) and payload.get("status") == "ready":
                if image_tag and revision != image_tag:
                    last_error = "El dominio no está respondiendo desde la revisión candidata"
                elif forbidden_revision and revision == forbidden_revision:
                    last_error = "El dominio aún responde desde el release candidato"
                else:
                    result = {"status": status, "ready": True, "domain": domain}
                    if revision:
                        result["image_tag"] = revision
                    return result
            else:
                last_error = "El dominio no devolvió readiness"
        if attempt < max(0, retries):
            time.sleep(max(0, retry_delay))
    raise GateError(last_error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=(
        "config-check", "preflight", "inventory", "deploy", "verify",
        "activate", "activate-develop", "disable-develop", "restore-legacy",
        "backup", "backup-verify",
    ))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument(
        "--preloaded-images",
        action="store_true",
        help="Verifica las imágenes locales fijadas en el manifiesto y despliega sin descargarlas",
    )
    parser.add_argument(
        "--replace-active",
        action="store_true",
        help=(
            "Permite que deploy reemplace el candidato de producción que ya recibe tráfico. "
            "Mantiene el respaldo, el drenaje de la cola y la verificación posterior"
        ),
    )
    parser.add_argument(
        "--allow-uncertain",
        action="store_true",
        help=(
            "En backup y deploy, no espera a las operaciones o jobs en estado uncertain "
            "(ningún worker los ejecuta); se conservan intactos y se registran en la evidencia"
        ),
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Directorio nuevo (backup) o existente (backup-verify) fuera del checkout",
    )
    args = parser.parse_args(argv)
    if args.action in {"backup", "backup-verify"} and args.backup_dir is None:
        parser.error("--backup-dir es obligatorio para backup y backup-verify")
    if args.allow_uncertain and args.action not in {"backup", "deploy"}:
        parser.error("--allow-uncertain solo se permite con backup o deploy")
    if args.replace_active and args.action != "deploy":
        parser.error("--replace-active solo se permite con la acción deploy")
    if args.preloaded_images and args.action != "deploy":
        parser.error("--preloaded-images solo se permite con la acción deploy")
    evidence = None
    result = {"action": args.action, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": socket.gethostname()}
    try:
        evidence = _new_evidence_dir(args.evidence_dir)
        data, compose, env_file = _manifest(
            args.manifest,
            require_runtime=args.action not in {"restore-legacy", "disable-develop", "backup-verify"},
        )
        result["manifest_sha256"] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
        if args.action == "restore-legacy":
            result["activation"] = _restore_legacy(data, evidence)
            result["external_health"] = _check_external(
                data["domain"], None, forbidden_revision=str(data.get("image_tag", ""))
            )
            result["passed"] = True
        elif args.action == "disable-develop":
            result["activation"] = _disable_develop(data, evidence)
            result["passed"] = True
        elif args.action == "backup-verify":
            result["restore_check"] = _backup_verify(args.backup_dir)
            result["passed"] = True
        else:
            config = _compose_config(data, compose, env_file)
            if args.action == "config-check":
                result["passed"] = True
                result["project"] = data["project"]
            elif args.action == "preflight":
                result["configuration"] = _check_policy(data, config, env_file)
                state_backend = _check_runtime_backend(data, config)
                if state_backend is not None:
                    result["state_backend"] = state_backend
                result["passed"] = True
            elif args.action == "backup":
                result["backup"] = _backup(
                    data, compose, env_file, args.backup_dir, allow_uncertain=args.allow_uncertain
                )
                result["passed"] = True
            elif args.action == "inventory":
                result["inventory"] = _inventory(data, compose, env_file)
                result["passed"] = True
            elif args.action == "deploy":
                result["configuration"] = _check_policy(data, config, env_file)
                state_backend = _check_runtime_backend(data, config)
                if state_backend is not None:
                    result["state_backend"] = state_backend
                if args.replace_active:
                    result["replace_active"] = True
                else:
                    _assert_production_candidate_inactive(data)
                prefix = _compose_args(data, compose, env_file)
                with _runtime_write_gate(prefix):
                    result["drain"] = _wait_for_stack_quiescence(
                        prefix, allow_uncertain=args.allow_uncertain
                    )
                    if args.preloaded_images:
                        result["preloaded_images"] = _verify_preloaded_images(config)
                    else:
                        _run(prefix + ["pull"], label="Descarga de imágenes", timeout=900)
                    _run(prefix + ["up", "-d", "--no-build", "--pull", "never"], label="Inicio del stack", timeout=900)
                    result["verification"] = _verify(data, compose, env_file)
                result["passed"] = True
                result["project"] = data["project"]
            elif args.action == "verify":
                result["configuration"] = _check_policy(data, config, env_file)
                state_backend = _check_runtime_backend(data, config)
                if state_backend is not None:
                    result["state_backend"] = state_backend
                result["verification"] = _verify(data, compose, env_file)
                result["passed"] = True
            elif args.action == "activate":
                result["configuration"] = _check_policy(data, config, env_file)
                state_backend = _check_runtime_backend(data, config)
                if state_backend is not None:
                    result["state_backend"] = state_backend
                result["verification"] = _verify(data, compose, env_file)
                result["activation"] = _activate_production(data, evidence)
                try:
                    result["external_health"] = _check_external(data["domain"], data["image_tag"])
                except GateError as activation_error:
                    try:
                        rollback = _rollback_failed_activation(data, evidence)
                        result["rollback"] = rollback
                        result["automatic_rollback"] = rollback["restored"]
                    except GateError as rollback_error:
                        raise GateError(
                            f"{activation_error}; no se confirmó el rollback seguro ({rollback_error})"
                        ) from rollback_error
                    try:
                        result["rollback_external_health"] = _check_external(
                            data["domain"], None,
                            forbidden_revision=str(data.get("image_tag", "")),
                        )
                    except GateError as rollback_error:
                        raise GateError(
                            f"{activation_error}; se restauró o confirmó la ruta legacy pero no su readiness ({rollback_error})"
                        ) from rollback_error
                    raise
                result["passed"] = True
            elif args.action == "activate-develop":
                result["configuration"] = _check_policy(data, config, env_file)
                result["verification"] = _verify(data, compose, env_file)
                result["activation"] = _activate_develop(data, evidence)
                result["passed"] = True
        _write_evidence(evidence, result)
        print(json.dumps({"passed": True, "action": args.action, "evidence_dir": str(evidence)}))
        return 0
    except Exception as error:
        result["passed"] = False
        result["reason"] = str(error) if type(error) is GateError else type(error).__name__
        if evidence is not None:
            _write_evidence(evidence, result)
        print(json.dumps({"passed": False, "action": args.action, "reason": result["reason"], "evidence_dir": str(evidence) if evidence else None}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
