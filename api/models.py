import os
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


NAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
TAG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"


class Command(str, Enum):
    plan = "plan"
    provision = "provision"
    apply = "apply"
    destroy = "destroy"
    power = "power"
    reconcile = "reconcile"


class Action(str, Enum):
    provision = "provision"
    destroy = "destroy"
    power = "power"
    reconcile = "reconcile"


class PowerAction(str, Enum):
    start = "start"
    shutdown = "shutdown"
    reboot = "reboot"


Environment = Literal["lab", "production"]
NetworkProfile = Literal["lab_dhcp"]


def _validate_ssh_keys(values: list[str]) -> list[str]:
    for value in values:
        if not value.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
            raise ValueError("ssh_public_keys debe contener claves SSH públicas")
    return values


class AccessRequest(BaseModel):
    """Datos de acceso inicial aceptados por el contrato estable v1."""

    root_password: SecretStr | None = Field(default=None, min_length=1)
    ssh_public_keys: list[str] = Field(default_factory=list)

    @field_validator("root_password")
    @classmethod
    def valid_root_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        minimum = max(1, int(os.getenv("IACP_ROOT_PASSWORD_MIN_LENGTH", "1")))
        secret = value.get_secret_value()
        if len(secret) < minimum:
            raise ValueError(f"root_password debe tener al menos {minimum} caracteres")
        return value

    @field_validator("ssh_public_keys")
    @classmethod
    def valid_keys(cls, values: list[str]) -> list[str]:
        return _validate_ssh_keys(values)


class InstanceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    image: str = Field(default="ubuntu-2404", pattern=r"^(ubuntu-2404|ubuntu-2604|almalinux-9|almalinux-10)$")
    vcpus: int = Field(default=2, ge=1, le=8)
    memory_mb: int = Field(default=2048, ge=512, le=16384)
    disk_gb: int = Field(default=20, ge=20, le=400)
    network_profile: NetworkProfile = "lab_dhcp"
    network_mode: Literal["dhcp"] = "dhcp"
    ssh_public_keys: list[str] = Field(default_factory=list)
    started: bool = True
    tags: list[str] = Field(
        default_factory=list,
        description="Tags Proxmox explícitos del consumidor. Si se omite o queda vacío, no se agrega ningún tag.",
        examples=[["cl-105", "kvm-001", "postal"]],
    )

    @field_validator("ssh_public_keys")
    @classmethod
    def valid_keys(cls, values: list[str]) -> list[str]:
        return _validate_ssh_keys(values)

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or not re.fullmatch(TAG_PATTERN, value):
                raise ValueError("tags debe contener etiquetas en minúsculas y con formato Proxmox válido")
        return list(dict.fromkeys(values))

class InstanceCreateRequest(BaseModel):
    """Solicitud estable del consumidor para aprovisionar una instancia."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "cliente-cl-0000",
                    "environment": "lab",
                    "plan_id": "kvm-2",
                    "image_id": "ubuntu-2404",
                    "network_profile": "lab_dhcp",
                    "tags": ["cl-105", "kvm-001", "postal"],
                    "access": {
                        "root_password": "CAMBIAR_POR_SECRETO",
                        "ssh_public_keys": [],
                    },
                    "start_on_create": True,
                }
            ]
        }
    )

    name: str = Field(pattern=NAME_PATTERN)
    environment: Environment | None = Field(
        default=None,
        description="Destino lógico publicado por GET /v1/environments; el servidor resuelve clúster y nodo.",
    )
    plan_id: str = Field(min_length=1, max_length=32)
    image_id: str = Field(min_length=1, max_length=64)
    network_profile: NetworkProfile = "lab_dhcp"
    tags: list[str] = Field(
        default_factory=list,
        description="Lista libre de tags Proxmox descriptivos. Solo se aplican los valores que envía el consumidor.",
        examples=[["cl-105", "kvm-001", "postal"]],
    )
    access: AccessRequest = Field(default_factory=AccessRequest)
    start_on_create: bool = True

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or not re.fullmatch(TAG_PATTERN, value):
                raise ValueError("tags debe contener etiquetas en minúsculas y con formato Proxmox válido")
        return list(dict.fromkeys(values))


class ProvisionRequest(BaseModel):
    plan_operation_id: str | None = None


class PowerRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"action": "start"}, {"action": "shutdown"}, {"action": "reboot"}]}
    )

    action: PowerAction


class OperationRequest(BaseModel):
    """Internal/operator request retained for explicit plan/apply control."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "command": "plan",
                    "action": "provision",
                    "environment": "lab",
                    "plan_id": "kvm-2",
                    "spec": {
                        "name": "iacp-api-demo",
                        "image": "ubuntu-2404",
                        "vcpus": 2,
                        "memory_mb": 4096,
                        "disk_gb": 100,
                        "network_mode": "dhcp",
                        "started": True,
                        "ssh_public_keys": [],
                    },
                },
                {
                    "command": "apply",
                    "action": "provision",
                    "plan_operation_id": "<operation_id_del_plan>",
                },
                {
                    "command": "destroy",
                    "action": "destroy",
                    "plan_operation_id": "<operation_id_del_plan>",
                },
                {
                    "command": "power",
                    "action": "power",
                    "instance_id": "<instance_id>",
                    "power_action": "shutdown",
                },
                {
                    "command": "reconcile",
                    "action": "reconcile",
                    "instance_id": "<instance_id>",
                    "source_operation_id": "<uncertain_operation_id>",
                },
            ]
        }
    )

    command: Command
    action: Action = Action.provision
    plan_id: str | None = None
    spec: InstanceSpec | None = None
    instance_id: str | None = None
    plan_operation_id: str | None = None
    power_action: PowerAction | None = None
    source_operation_id: str | None = None
    environment: Environment | None = None

    @model_validator(mode="after")
    def valid_command(self):
        if self.command == Command.power:
            if self.action != Action.power:
                raise ValueError("power requiere action=power")
            if not self.instance_id:
                raise ValueError("power requiere instance_id")
            if self.power_action is None:
                raise ValueError("power requiere power_action")
            if self.spec is not None or self.plan_operation_id is not None:
                raise ValueError("power no acepta spec ni plan_operation_id")
            return self
        if self.command == Command.reconcile:
            if self.action != Action.reconcile:
                raise ValueError("reconcile requiere action=reconcile")
            if not self.instance_id:
                raise ValueError("reconcile requiere instance_id")
            if self.spec is not None or self.plan_operation_id is not None or self.power_action is not None:
                raise ValueError("reconcile no acepta spec, plan_operation_id ni power_action")
            return self
        if self.source_operation_id is not None:
            raise ValueError("source_operation_id solo aplica a reconcile")
        if self.power_action is not None:
            raise ValueError("power_action solo aplica a command=power")
        if self.command == Command.provision:
            if self.action != Action.provision:
                raise ValueError("provision requiere action=provision")
            if self.spec is None:
                raise ValueError("spec es obligatorio para provision")
            if not self.plan_id:
                raise ValueError("plan_id es obligatorio para provision")
            return self
        if self.command == Command.plan and self.action == Action.provision and self.spec is None:
            raise ValueError("spec es obligatorio para plan de provision")
        if self.command == Command.apply and not self.plan_operation_id:
            raise ValueError("apply requiere plan_operation_id")
        if self.command == Command.destroy and not (self.plan_operation_id or self.instance_id):
            raise ValueError("destroy requiere plan_operation_id o instance_id")
        return self
