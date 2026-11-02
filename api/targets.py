"""Enrutamiento interno entre entornos lógicos y perfiles Proxmox.

El consumidor solo declara ``lab`` o ``production``. Los nodos, credenciales,
catálogos y directorios Terraform permanecen en la configuración del servidor.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit


class EnvironmentError(ValueError):
    """Error seguro de configuración o disponibilidad de un entorno."""

    def __init__(self, code: str, *, status_code: int = 409):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


ENVIRONMENTS = {"lab", "production"}


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def _configured(value: str | None) -> bool:
    normalized = str(value or "").strip()
    lowered = normalized.lower()
    return bool(normalized) and not any(
        marker in lowered for marker in ("reemplazar", "replace_me", "placeholder", "example")
    )


def _valid_https_endpoint(value: str | None) -> bool:
    if not _configured(value):
        return False
    try:
        parsed = urlsplit((value or "").strip())
        return parsed.scheme == "https" and bool(parsed.hostname) and not any(
            marker in parsed.hostname.lower() for marker in ("reemplazar", "placeholder", "example")
        )
    except ValueError:
        return False


def _valid_proxmox_credentials(endpoint: str, token: str, credential_file: str) -> bool:
    if _valid_https_endpoint(endpoint) and _configured(token):
        return True
    if not credential_file or not Path(credential_file).is_file():
        return False
    try:
        data = json.loads(Path(credential_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    file_endpoint = data.get("PROXMOX_VE_ENDPOINT") or data.get("endpoint")
    file_token = data.get("PROXMOX_VE_API_TOKEN") or data.get("api_token")
    return _valid_https_endpoint(str(file_endpoint or "")) and _configured(str(file_token or ""))


def _catalog_ready(path: str, *, allow_single_node: bool = False) -> bool:
    if not path or not Path(path).is_file():
        return False
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        plans = data.get("plans")
        images = data.get("images")
        nodes = data.get("nodes")
        if allow_single_node and not nodes:
            nodes = [data.get("node")]
        return bool(
            _configured(data.get("storage"))
            and isinstance(nodes, list)
            and nodes
            and all(_configured(node) for node in nodes)
            and isinstance(plans, list)
            and plans
            and all(
                isinstance(plan, dict)
                and all(key in plan for key in ("plan_id", "vcpus", "memory_mb", "disk_gb"))
                for plan in plans
            )
            and isinstance(images, list)
            and images
            and all(
                isinstance(image, dict)
                and all(key in image for key in ("image_id", "template_vm_id", "source_node", "storage", "tag"))
                for image in images
            )
        )
    except (OSError, ValueError, TypeError):
        return False


def _state_backend_ready() -> bool:
    stage = os.getenv("IACP_RUNTIME_STAGE", "develop").strip().lower()
    backend = os.getenv("IACP_TERRAFORM_BACKEND", "local").strip().lower()
    if stage == "develop" and backend == "local":
        return True
    if stage != "production" or backend != "s3":
        return False
    return bool(
        os.getenv("IACP_TFSTATE_PREFIX", "").strip().strip("/") == "iacp/prod-runtime-v1"
        and _configured(os.getenv("IACP_TFSTATE_BUCKET"))
        and _valid_https_endpoint(os.getenv("IACP_TFSTATE_ENDPOINT"))
        and _configured(os.getenv("IACP_TFSTATE_REGION"))
    )


def _lab_ready() -> bool:
    endpoint = os.getenv("IACP_PROXMOX_MNG_ENDPOINT", "")
    token = os.getenv("IACP_PROXMOX_MNG_API_TOKEN", "")
    credential_file = os.getenv("IACP_PROXMOX_MNG_CREDENTIALS_FILE", "/run/secrets/proxmox_credentials")
    terraform_root = Path(os.getenv("IACP_TERRAFORM_PATH", "/app/terraform"))
    terraform_dir = os.getenv("IACP_TERRAFORM_MNG_DIR", "environments/mng01")
    default_catalog = Path(__file__).resolve().parent.parent / "terraform" / "environments" / "mng01" / "catalog.json"
    catalog = os.getenv("IACP_MNG_CATALOG", str(default_catalog))
    return bool(
        _state_backend_ready()
        and _valid_proxmox_credentials(endpoint, token, credential_file)
        and _configured(os.getenv("IACP_PROXMOX_MNG_POOL_ID", "iacp-lab"))
        and (terraform_root / terraform_dir).is_dir()
        and _catalog_ready(catalog, allow_single_node=True)
    )


def _production_ready() -> bool:
    if not _enabled(os.getenv("IACP_PRX_ENABLED", "0")):
        return False
    catalog = os.getenv("IACP_PRX_CATALOG", "").strip()
    terraform_dir = os.getenv("IACP_TERRAFORM_PRX_DIR", "").strip()
    terraform_root = Path(os.getenv("IACP_TERRAFORM_PATH", "/app/terraform"))
    credential_file = os.getenv("IACP_PROXMOX_PRX_CREDENTIALS_FILE", "").strip()
    credentials_ready = _valid_proxmox_credentials(
        os.getenv("IACP_PROXMOX_PRX_ENDPOINT", ""),
        os.getenv("IACP_PROXMOX_PRX_API_TOKEN", ""),
        credential_file,
    )
    state_ready = (
        _state_backend_ready()
        and os.getenv("IACP_TFSTATE_PREFIX", "").strip().strip("/") == "iacp/prod-runtime-v1"
    )
    return bool(
        credentials_ready
        and _configured(os.getenv("IACP_PROXMOX_PRX_POOL_ID"))
        and _catalog_ready(catalog)
        and terraform_dir
        and (terraform_root / terraform_dir).is_dir()
        and state_ready
    )


def default_environment() -> str:
    value = os.getenv("IACP_DEFAULT_ENVIRONMENT", "lab").strip().lower()
    if value not in ENVIRONMENTS:
        raise EnvironmentError("ENVIRONMENT_CONFIGURATION_INCOMPLETE", status_code=503)
    return value


def enabled_environments() -> set[str]:
    configured = os.getenv("IACP_ENABLED_ENVIRONMENTS", "lab")
    result = {item.strip().lower() for item in configured.split(",") if item.strip()}
    if not result or not result <= ENVIRONMENTS:
        raise EnvironmentError("ENVIRONMENT_CONFIGURATION_INCOMPLETE", status_code=503)
    if not _state_backend_ready():
        raise EnvironmentError("STATE_BACKEND_CONFIGURATION_INCOMPLETE", status_code=503)
    if "lab" in result and not _lab_ready():
        raise EnvironmentError("MNG_CONFIGURATION_INCOMPLETE", status_code=503)
    if "production" in result and not _production_ready():
        result.remove("production")
    if not result:
        raise EnvironmentError("ENVIRONMENT_CONFIGURATION_INCOMPLETE", status_code=503)
    return result


def resolve_environment(value: str | None = None) -> str:
    configured_default = default_environment()
    enabled = enabled_environments()
    environment = (value or configured_default).strip().lower()
    if environment not in ENVIRONMENTS:
        raise EnvironmentError("ENVIRONMENT_INVALID", status_code=422)
    if value is None and environment not in enabled:
        raise EnvironmentError("ENVIRONMENT_CONFIGURATION_INCOMPLETE", status_code=503)
    if environment not in enabled:
        raise EnvironmentError("ENVIRONMENT_DISABLED", status_code=409)
    return environment


def public_environments() -> dict:
    """Exponer nombres lógicos habilitados sin revelar la ruta física interna."""

    enabled = enabled_environments()
    default = default_environment()
    if default not in enabled:
        raise EnvironmentError("ENVIRONMENT_CONFIGURATION_INCOMPLETE", status_code=503)
    labels = {"lab": "Laboratorio", "production": "Producción"}
    return {
        "default_environment": default,
        "environments": [
            {"environment": name, "label": labels[name]}
            for name in ("lab", "production")
            if name in enabled
        ],
    }


def terraform_target_for_environment(environment: str) -> str:
    return "prx" if environment == "production" else "mng01"


def catalog_path(environment: str | None = None) -> Path:
    selected = resolve_environment(environment)
    if selected == "lab":
        default = Path(__file__).resolve().parent.parent / "terraform" / "environments" / "mng01" / "catalog.json"
        return Path(os.getenv("IACP_MNG_CATALOG", str(default)))

    configured = os.getenv("IACP_PRX_CATALOG", "").strip()
    if not configured:
        raise EnvironmentError("ENVIRONMENT_CONFIGURATION_INCOMPLETE", status_code=503)
    path = Path(configured)
    if not path.is_file():
        raise EnvironmentError("ENVIRONMENT_CONFIGURATION_INCOMPLETE", status_code=503)
    return path


def terraform_directory(environment: str) -> str:
    environment = resolve_environment(environment)
    if environment == "production":
        configured = os.getenv("IACP_TERRAFORM_PRX_DIR", "").strip()
        if not configured:
            raise EnvironmentError("ENVIRONMENT_CONFIGURATION_INCOMPLETE", status_code=503)
        return configured
    return os.getenv("IACP_TERRAFORM_MNG_DIR", "environments/mng01")


def proxmox_credentials(environment: str) -> tuple[str, str]:
    environment = resolve_environment(environment)
    if environment == "production":
        endpoint = os.getenv("IACP_PROXMOX_PRX_ENDPOINT", "").strip()
        token = os.getenv("IACP_PROXMOX_PRX_API_TOKEN", "").strip()
        credential_file = os.getenv("IACP_PROXMOX_PRX_CREDENTIALS_FILE", "").strip()
    else:
        endpoint = os.getenv("IACP_PROXMOX_MNG_ENDPOINT", "").strip()
        token = os.getenv("IACP_PROXMOX_MNG_API_TOKEN", "").strip()
        credential_file = os.getenv("IACP_PROXMOX_MNG_CREDENTIALS_FILE", "/run/secrets/proxmox_credentials").strip()
    if not endpoint or not token:
        try:
            with open(credential_file, encoding="utf-8") as handle:
                data = json.load(handle)
            endpoint = endpoint or str(data.get("PROXMOX_VE_ENDPOINT") or data.get("endpoint") or "").strip()
            token = token or str(data.get("PROXMOX_VE_API_TOKEN") or data.get("api_token") or "").strip()
        except (OSError, ValueError, TypeError):
            pass
    if not endpoint or not token:
        raise EnvironmentError("PROXMOX_CREDENTIALS_UNAVAILABLE", status_code=503)
    if not endpoint.startswith("https://"):
        raise EnvironmentError("PROXMOX_ENDPOINT_INVALID", status_code=503)
    return endpoint.rstrip("/"), token


def pool_id(environment: str) -> str:
    if environment == "production":
        value = os.getenv("IACP_PROXMOX_PRX_POOL_ID", "").strip()
        if not value:
            raise EnvironmentError("ENVIRONMENT_CONFIGURATION_INCOMPLETE", status_code=503)
        return value
    return os.getenv("IACP_PROXMOX_MNG_POOL_ID", "iacp-lab").strip()
