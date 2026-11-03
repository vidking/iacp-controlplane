"""Configuración del entorno lógico expuesto por la API de IAC.

La API mantiene el contrato público ``lab`` y resuelve MNG internamente. La
selección del nodo Proxmox permanece en el worker y no forma parte del JSON.
"""

from __future__ import annotations

import os


class EnvironmentConfigurationError(RuntimeError):
    def __init__(self, code: str, status_code: int = 503):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def resolve_environment(value: str | None = None) -> str:
    default = os.getenv("IACP_DEFAULT_ENVIRONMENT", "lab").strip().lower()
    enabled = {
        item.strip().lower()
        for item in os.getenv("IACP_ENABLED_ENVIRONMENTS", "lab").split(",")
        if item.strip()
    }
    if default != "lab" or enabled != {"lab"}:
        raise EnvironmentConfigurationError("ENVIRONMENT_CONFIGURATION_INCOMPLETE")

    requested = (value or default).strip().lower()
    if requested != "lab":
        raise EnvironmentConfigurationError("ENVIRONMENT_DISABLED", status_code=409)
    return "lab"


def public_environment_catalog() -> dict:
    default = resolve_environment()
    return {
        "default_environment": default,
        "environments": [{"environment": "lab", "label": "Laboratorio"}],
    }
