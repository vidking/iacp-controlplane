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
import socket
import subprocess
import tempfile
import time
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
    return [
        "docker", "compose", "--project-name", data["project"],
        "--env-file", str(env_file), "-f", str(compose),
    ]


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
            "domain": "dev-cyberfuel.com",
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
    expected_vmid_range = (expected_identity["vmid_min"], expected_identity["vmid_max"])
    if (data.get("vmid_min"), data.get("vmid_max")) != expected_vmid_range:
        raise GateError(f"El rango VMID no coincide con el entorno {stage}")
    if data["proxy_network"] != "proxy_network":
        raise GateError("La red compartida debe ser proxy_network")
    api_env = _service_env(config, "api")
    worker_envs = [_service_env(config, "worker-1"), _service_env(config, "worker-2")]
    postgres_env = _service_env(config, "postgres")
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
    if str(api_env.get("IACP_MAX_CONCURRENT", "")) != "1" or data["max_concurrent"] != 1:
        raise GateError("Cada stack debe limitarse a una operación simultánea")
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
    if api_env.get("IACP_REQUIRE_API_TOKEN") not in {"1", 1, True}:
        raise GateError("La API debe exigir Bearer token")
    for key in ("IACP_API_TOKEN", "IACP_PROXMOX_MNG_API_TOKEN"):
        value = api_env.get(key)
        if not isinstance(value, str) or len(value.strip()) < 16 or "REEMPLAZAR" in value.upper():
            raise GateError(f"Falta configurar {key}")
    database_password = postgres_env.get("POSTGRES_PASSWORD")
    if not isinstance(database_password, str) or len(database_password.strip()) < 16 or "REEMPLAZAR" in database_password.upper():
        raise GateError("Falta configurar POSTGRES_PASSWORD")
    if postgres_env.get("POSTGRES_DB") != expected_identity["database"]:
        raise GateError("La base PostgreSQL no coincide con el entorno")
    if postgres_env.get("POSTGRES_USER") != api_env.get("PGUSER"):
        raise GateError("El usuario PostgreSQL no coincide entre API y base")
    for environment in (api_env, *worker_envs):
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
    for service_name in ("api", "worker-1", "worker-2"):
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
            if worker_envs[0].get(key) != worker_envs[1].get(key):
                raise GateError("Las credenciales Wasabi deben coincidir entre ambos workers")
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
    image_tags = set()
    for service in ("api", "worker-1", "worker-2"):
        image = config["services"][service].get("image", "")
        if not image.startswith(repository + ":"):
            raise GateError(f"La imagen de {service} no usa el repositorio aprobado")
        tag = image.rsplit(":", 1)[-1]
        if not COMMIT_TAG.fullmatch(tag):
            raise GateError(f"La imagen de {service} no está fijada a un commit inmutable")
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
        "max_concurrent_per_stack": 1,
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
        "Host(`dev-cyberfuel.com`) && (!HeaderRegexp(`Origin`, `.+`) || "
        "HeaderRegexp(`Origin`, `^(?:https://dev-cyberfuel\\.com)$`))"
    )
    expected_middlewares = [
        "internal-sources@file", "strip-identity@file", "root-tls-transport@file",
        "request-rate@file", "inflight@file", "body-limit@file", "iacp-dev-headers",
    ]
    expected_headers = {
        "accessControlAllowOriginList": ["https://dev-cyberfuel.com"],
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
        or http_router.get("rule") != "Host(`dev-cyberfuel.com`)"
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
    expected = {"postgres", "api", "worker-1", "worker-2"}
    if {item["service"] for item in statuses} != expected:
        raise GateError("No están activos exactamente los cuatro servicios esperados")
    if any(item["state"] != "running" for item in statuses):
        raise GateError("Hay servicios del stack que no están running")
    postgres = next(item for item in statuses if item["service"] == "postgres")
    api = next(item for item in statuses if item["service"] == "api")
    if postgres["health"] != "healthy" or api["health"] != "healthy":
        raise GateError("PostgreSQL o la API no está healthy")
    script = (
        "import json,urllib.request; "
        "response=urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=5); "
        "health=json.load(response); revision=response.headers.get('X-IACP-Build-Revision',''); "
        "spec=json.load(urllib.request.urlopen('http://127.0.0.1:8080/openapi.json',timeout=5)); "
        "print(json.dumps({'health':health.get('status'),'api_version':spec.get('info',{}).get('version'),"
        "'provision_path':'/v1/instances' in spec.get('paths',{}),'build_revision':revision}))"
    )
    result = _run(prefix + ["exec", "-T", "api", "python", "-c", script], label="Verificación API")
    try:
        payload = json.loads(result.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as error:
        raise GateError("La verificación API no devolvió JSON válido") from error
    expected_revision = str(data.get("image_tag", ""))
    if payload != {
        "health": "ready",
        "api_version": "1.0.0",
        "provision_path": True,
        "build_revision": expected_revision,
    }:
        raise GateError("La API no cumple readiness, contrato V1 y revisión de imagen")
    return {"services": statuses, "api": payload}


def _verify_preloaded_images(config: dict) -> list[dict[str, str]]:
    services = config.get("services", {})
    verified = []
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
        verified.append({"service": service_name, "image": image, "image_id": image_id})
    if not verified:
        raise GateError("La configuración no contiene imágenes para el despliegue precargado")
    return verified


def _activate_production(data: dict, evidence: Path) -> dict:
    if data["stage"] != "production":
        raise GateError("La activación productiva requiere el manifiesto production")
    certification_path = Path(data.get("certification_file", ""))
    if not certification_path.is_file():
        raise GateError("Falta la certificación física PASS ligada a esta imagen")
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
    if not required.issubset(results) or any(results.get(key) != "PASS" for key in required):
        raise GateError("La certificación no cubre todos los gates productivos")
    route = Path(data["dynamic_config"]).resolve()
    target = f"http://{data['service_alias']}:8080"
    update = _swap_upstream(route, evidence, "http://api:8000", target)
    return {
        "activated": update["changed"],
        "already_active": update.get("already_active", False),
        "target": target,
        "automatic_rollback": False,
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


def _develop_route_paths(data: dict) -> tuple[Path, Path, Path, Path]:
    if (
        data.get("stage") != "develop"
        or data.get("domain") != "dev-cyberfuel.com"
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
    ))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument(
        "--preloaded-images",
        action="store_true",
        help="Verifica las imágenes locales fijadas en el manifiesto y despliega sin descargarlas",
    )
    args = parser.parse_args(argv)
    if args.preloaded_images and args.action != "deploy":
        parser.error("--preloaded-images solo se permite con la acción deploy")
    evidence = None
    result = {"action": args.action, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": socket.gethostname()}
    try:
        evidence = _new_evidence_dir(args.evidence_dir)
        data, compose, env_file = _manifest(
            args.manifest,
            require_runtime=args.action not in {"restore-legacy", "disable-develop"},
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
            elif args.action == "inventory":
                result["inventory"] = _inventory(data, compose, env_file)
                result["passed"] = True
            elif args.action == "deploy":
                result["configuration"] = _check_policy(data, config, env_file)
                state_backend = _check_runtime_backend(data, config)
                if state_backend is not None:
                    result["state_backend"] = state_backend
                prefix = _compose_args(data, compose, env_file)
                if args.preloaded_images:
                    result["preloaded_images"] = _verify_preloaded_images(config)
                else:
                    _run(prefix + ["pull"], label="Descarga de imágenes", timeout=900)
                _run(prefix + ["up", "-d", "--no-build", "--pull", "never"], label="Inicio del stack", timeout=900)
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
                result["external_health"] = _check_external(data["domain"], data["image_tag"])
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
