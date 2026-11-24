"""Diagnósticos seguros para fallos del ejecutor Ansible de servicios."""

from __future__ import annotations

import re


TASK_CODES = {
    "Esperar cloud-init": "CLOUD_INIT",
    "Retirar clave temporal": "SSH_HANDOFF",
    "Validar Ubuntu Server 24.04": "SERVICE_OS",
    "Instalar dependencias de Ubuntu 24.04": "DOCKER_DEPENDENCIES",
    "Iniciar Docker": "DOCKER_DAEMON",
    "Crear directorio": "SERVICE_DIRECTORY",
    "Crear datos": "SERVICE_DIRECTORY",
    "Escribir configuración": "SERVICE_CONFIGURATION",
    "Descargar imágenes de aplicación": "APPLICATION_IMAGE_PULL",
    "Configurar gateway": "AGENT_CONFIGURATION",
    "Seleccionar modelo": "AGENT_MODEL",
    "Arrancar aplicación": "APPLICATION_START",
    "Esperar instalación Nextcloud": "NEXTCLOUD_INSTALL",
    "Autorizar hostname de acceso de Nextcloud": "NEXTCLOUD_DOMAIN",
    "Verificar hostname confiable de Nextcloud": "NEXTCLOUD_DOMAIN",
    "Resolver proxy": "PROXY_DISCOVERY",
    "Confiar en el proxy asignado": "PROXY_CONFIGURATION",
    "Preparar proxy cerrado para la instalación": "PROXY_CONFIGURATION",
    "Publicar configuración verificada del proxy": "PROXY_CONFIGURATION",
    "Reiniciar proxy con configuración verificada": "PROXY_CONFIGURATION",
    "Verificar Nextcloud": "NEXTCLOUD_VERIFICATION",
    "Verificación funcional revisada": "APPLICATION_FUNCTIONAL",
    "Verificar HTTPS": "HTTPS_VALIDATION",
    "Validar DNS y egress HTTPS antes de instalar dependencias": (
        "SERVICE_EGRESS_PREFLIGHT"
    ),
    "Adquirir exclusión remota": "REMOTE_LOCK",
    "Liberar exclusión remota": "REMOTE_LOCK",
    "Esperar conectividad SSH": "SSH_CONNECTIVITY",
    "Recopilar facts del invitado": "GUEST_FACTS",
}

_SAFE_CODES = frozenset(TASK_CODES.values()) | {"OTHER"}
_EVENT = re.compile(r"^IACP_(TASK|FAILED|UNREACHABLE):([A-Z0-9_]+)$")


class ServiceExecutionUncertain(RuntimeError):
    """Un cambio remoto pudo comenzar y requiere recuperación explícita."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ServiceExecutionFailed(RuntimeError):
    """Falló una fase previa al arranque que se puede reintentar sin ambigüedad."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def task_code(name: str) -> str:
    """Devuelve un identificador allowlist; nunca devuelve entrada arbitraria."""

    return TASK_CODES.get(name.strip(), "OTHER")


def safe_execution_error(
    events: str, *, return_code: int | None, timed_out: bool = False
) -> str:
    """Resume eventos del callback sin conservar salida de módulos ni secretos."""

    last_task = "OTHER"
    for line in events.splitlines():
        match = _EVENT.fullmatch(line.strip())
        if not match:
            continue
        event, raw_code = match.groups()
        code = raw_code if raw_code in _SAFE_CODES else "OTHER"
        if event == "TASK":
            last_task = code
        elif event in {"FAILED", "UNREACHABLE"}:
            # This is the first pre-task. No service mutation has started yet.
            if code == "SSH_CONNECTIVITY":
                return "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_SSH_CONNECTIVITY"
            if event == "FAILED" and code == "SERVICE_EGRESS_PREFLIGHT":
                return "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_SERVICE_EGRESS_PREFLIGHT"
            if event == "FAILED" and code == "APPLICATION_IMAGE_PULL":
                return "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_APPLICATION_IMAGE_PULL"
            return f"SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_{event}_{code}"
    if timed_out:
        return f"SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_TIMEOUT_{last_task}"
    if return_code:
        return f"SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_EXIT_{last_task}"
    return "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_RESULT_UNKNOWN"
