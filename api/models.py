from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class Command(str, Enum):
    plan = "plan"
    apply = "apply"
    destroy = "destroy"
    power = "power"


class Action(str, Enum):
    provision = "provision"
    destroy = "destroy"
    power = "power"


class PowerAction(str, Enum):
    start = "start"
    shutdown = "shutdown"
    reboot = "reboot"


class InstanceSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
    image: str = Field(default="ubuntu-2404", pattern=r"^(ubuntu-2404|ubuntu-2604|almalinux-9|almalinux-10)$")
    plan_id: str | None = Field(default=None, pattern=r"^kvm-[1-4]$")
    vcpus: int | None = Field(default=None, ge=1, le=8)
    memory_mb: int | None = Field(default=None, ge=512, le=16384)
    disk_gb: int | None = Field(default=None, ge=20, le=400)
    network_mode: Literal["dhcp", "static"] = "dhcp"
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    ipv4_cidr: str | None = None
    ipv4_gateway: str | None = None
    ssh_public_keys: list[str] = Field(default_factory=list)
    root_password: SecretStr | None = Field(
        default=None,
        repr=False,
        json_schema_extra={"writeOnly": True},
    )
    started: bool = True
    tags: list[str] = Field(default_factory=list)

    @field_validator("ssh_public_keys")
    @classmethod
    def valid_keys(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
                raise ValueError("ssh_public_keys debe contener claves SSH públicas")
        return values

    @field_validator("root_password")
    @classmethod
    def valid_root_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            secret = value.get_secret_value()
            if len(secret) < 12 or "\n" in secret or "\r" in secret:
                raise ValueError("root_password debe tener al menos 12 caracteres y no contener saltos de línea")
        return value

    @model_validator(mode="after")
    def valid_network(self):
        if self.network_mode == "static" and not (self.ipv4_cidr and self.ipv4_gateway):
            raise ValueError("network_mode=static requiere ipv4_cidr e ipv4_gateway")
        if self.network_mode == "dhcp" and (self.ipv4_cidr or self.ipv4_gateway):
            raise ValueError("network_mode=dhcp no acepta direccionamiento estatico")
        return self


class InstanceCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "stack": "mng01",
                    "spec": {
                        "name": "iacp-v2-demo",
                        "image": "ubuntu-2404",
                        "plan_id": "kvm-1",
                        "network_mode": "dhcp",
                        "started": False,
                        "ssh_public_keys": [],
                        "root_password": "SECRET_NOT_LOGGED",
                    },
                }
            ]
        }
    )

    stack: Literal["mng01", "local"] = "mng01"
    spec: InstanceSpec

    @model_validator(mode="after")
    def require_mng_access(self):
        if self.stack == "mng01":
            if self.spec.plan_id is None:
                raise ValueError("mng01 requiere plan_id del catalogo tecnico")
            if self.spec.root_password is None:
                raise ValueError("mng01 requiere root_password temporal para verificar acceso SSH root")
        return self


class ProvisionRequest(BaseModel):
    plan_operation_id: str | None = None


class PowerRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"action": "start"}, {"action": "shutdown"}, {"action": "reboot"}]}
    )

    action: PowerAction


class OperationRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "command": "plan",
                    "action": "provision",
                    "stack": "mng01",
                    "spec": {
                        "name": "iacp-v2-demo",
                        "image": "ubuntu-2404",
                        "plan_id": "kvm-1",
                        "network_mode": "dhcp",
                        "started": True,
                        "ssh_public_keys": [],
                        "root_password": "SECRET_NOT_LOGGED",
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
            ]
        }
    )
    command: Command
    action: Action = Action.provision
    spec: InstanceSpec | None = None
    instance_id: str | None = None
    plan_operation_id: str | None = None
    power_action: PowerAction | None = None
    stack: Literal["mng01", "local"] = "mng01"

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
        if self.power_action is not None:
            raise ValueError("power_action solo aplica a command=power")
        if self.command == Command.plan and self.action == Action.provision and self.spec is None:
            raise ValueError("spec es obligatorio para plan de provision")
        if self.command == Command.plan and self.stack == "mng01" and self.spec is not None:
            if self.spec.plan_id is None:
                raise ValueError("mng01 requiere plan_id del catalogo tecnico")
            if self.spec.root_password is None:
                raise ValueError("mng01 requiere root_password temporal para verificar acceso SSH root")
        if self.command == Command.apply and not self.plan_operation_id:
            raise ValueError("apply requiere plan_operation_id")
        if self.command == Command.destroy and not (self.plan_operation_id or self.instance_id):
            raise ValueError("destroy requiere plan_operation_id o instance_id")
        return self
