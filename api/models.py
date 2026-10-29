from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Command(str, Enum):
    plan = "plan"
    apply = "apply"
    destroy = "destroy"


class Action(str, Enum):
    provision = "provision"
    destroy = "destroy"


class InstanceSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
    image: str = Field(default="ubuntu-2404", pattern=r"^(ubuntu-2404|ubuntu-2604|almalinux-9|almalinux-10)$")
    vcpus: int = Field(default=2, ge=1, le=8)
    memory_mb: int = Field(default=2048, ge=512, le=16384)
    disk_gb: int = Field(default=20, ge=20, le=400)
    network_mode: Literal["dhcp", "static"] = "dhcp"
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    ipv4_cidr: str | None = None
    ipv4_gateway: str | None = None
    ssh_public_keys: list[str] = Field(default_factory=list)
    started: bool = True
    tags: list[str] = Field(default_factory=list)

    @field_validator("ssh_public_keys")
    @classmethod
    def valid_keys(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
                raise ValueError("ssh_public_keys debe contener claves SSH públicas")
        return values

    @model_validator(mode="after")
    def valid_network(self):
        if self.network_mode == "static" and not (self.ipv4_cidr and self.ipv4_gateway):
            raise ValueError("network_mode=static requiere ipv4_cidr e ipv4_gateway")
        if self.network_mode == "dhcp" and (self.ipv4_cidr or self.ipv4_gateway):
            raise ValueError("network_mode=dhcp no acepta direccionamiento estatico")
        return self

class OperationRequest(BaseModel):
    command: Command
    action: Action = Action.provision
    spec: InstanceSpec | None = None
    instance_id: str | None = None
    plan_operation_id: str | None = None
    stack: Literal["mng01", "local"] = "mng01"

    @model_validator(mode="after")
    def valid_command(self):
        if self.command == Command.plan and self.action == Action.provision and self.spec is None:
            raise ValueError("spec es obligatorio para plan de provision")
        if self.command == Command.apply and not self.plan_operation_id:
            raise ValueError("apply requiere plan_operation_id")
        if self.command == Command.destroy and not (self.plan_operation_id or self.instance_id):
            raise ValueError("destroy requiere plan_operation_id o instance_id")
        return self
