import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

SOURCE_REVISION = "40dc5d10387cfb50384ecd3ddddfd75289fe7bc4"
TERRAFORM_VERSION = "1.16.6"


class PlanError(Exception):
    def __init__(self, code: str, *, phase: str | None = None,
                 command: str | None = None, exit_code: int | None = None,
                 http_status: int | None = None):
        self.code = code
        self.phase = phase
        self.command = command
        self.exit_code = exit_code
        self.http_status = http_status
        self.state_present = None
        self.proxmox_present = None
        self.state_consistency = None
        super().__init__(code)

    def diagnostic(self) -> dict:
        """Metadatos operativos permitidos; nunca incluye stdout, stderr ni argumentos."""
        return {key: value for key, value in {
            "phase": self.phase, "command": self.command, "exit_code": self.exit_code,
            "http_status": self.http_status, "state_present": self.state_present,
            "proxmox_present": self.proxmox_present,
            "state_consistency": self.state_consistency,
        }.items() if value is not None}


class Cancelled(PlanError):
    def __init__(self):
        super().__init__("CANCELLED")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Backend(StrictModel):
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    region: str = Field(min_length=1)
    endpoint: str

    @model_validator(mode="after")
    def wasabi(self):
        url = urlsplit(self.endpoint)
        if (url.scheme != "https" or not (url.hostname or "").endswith(".wasabisys.com")
                or url.username or url.password or url.port or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError("Se requiere el endpoint HTTPS Wasabi autorizado")
        return self

    def arguments(self, key: str) -> dict:
        return {"bucket": self.bucket, "key": key, "region": self.region, "endpoints": {"s3": self.endpoint},
                "use_lockfile": True, "encrypt": True, "use_path_style": True,
                "skip_credentials_validation": True, "skip_requesting_account_id": True,
                "skip_metadata_api_check": True}


class BootDisk(StrictModel):
    interface: str = Field(pattern=r"^(scsi([0-9]|[12][0-9]|30)|sata[0-5]|virtio([0-9]|1[0-5]))$")
    datastore_id: str = Field(min_length=1)
    minimum_size_gb: int = Field(ge=1)
    file_format: Literal["raw", "qcow2", "vmdk"] | None = None
    aio: Literal["io_uring", "native", "threads"] = "io_uring"
    cache: Literal["none", "writethrough", "writeback", "directsync", "unsafe"] = "none"
    discard: Literal["on", "ignore"] = "ignore"
    backup: bool = True
    replicate: bool = True
    iothread: bool = False
    ssd: bool = False


class Allocation(StrictModel):
    image: str = Field(min_length=1)
    environment: Literal["lab"]
    allowed_nodes: list[str] = Field(min_length=1)
    backend: Backend
    vm_id: int = Field(ge=100, le=999999999)
    template_vm_id: int = Field(ge=100)
    node_name: str = Field(min_length=1)
    template_node_name: str | None = None
    pool_id: str | None = None
    clone_datastore_id: str | None = None
    network_bridge: str = Field(min_length=1)
    vlan_id: int = Field(ge=1, le=4094)
    ipv4_cidr: str
    ipv4_gateway: str
    cloudinit_datastore_id: str = Field(min_length=1)
    cloudinit_username: str = Field(min_length=1)
    ssh_public_keys: list[str] = Field(min_length=1)
    guest_agent_enabled: bool = False
    started: bool = False
    boot_disk: BootDisk

    @model_validator(mode="after")
    def authorized_target(self):
        from ipaddress import IPv4Interface, IPv4Address
        if 104 in {self.vm_id, self.template_vm_id} or self.vm_id == self.template_vm_id:
            raise ValueError("El destino y la plantilla deben excluir VM 104 y ser distintos")
        if self.node_name not in self.allowed_nodes or (self.template_node_name and self.template_node_name not in self.allowed_nodes):
            raise ValueError("El nodo debe estar autorizado")
        if not set(self.allowed_nodes).issubset({"mng01", "mng02"}):
            raise ValueError("Solo se admite el baseline MNG de laboratorio")
        IPv4Interface(self.ipv4_cidr)
        IPv4Address(self.ipv4_gateway)
        if not all(key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")) for key in self.ssh_public_keys):
            raise ValueError("Se requieren claves SSH públicas autorizadas")
        return self

    def variables(self, instance) -> dict:
        if instance.image != self.image:
            raise PlanError("AUTHORIZED_IMAGE_MISMATCH")
        if instance.disk_gb < self.boot_disk.minimum_size_gb:
            raise PlanError("DISK_SHRINK_FORBIDDEN")
        values = self.model_dump(exclude={"image", "environment", "allowed_nodes", "backend"})
        values["boot_disk"]["size_gb"] = instance.disk_gb
        values.update(vm_name=instance.name, cpu_cores=instance.vcpus, memory_mb=instance.memory_mb)
        return {"environment": self.environment, "allowed_nodes": self.allowed_nodes, "instance": values}


def allocation_for(instance) -> Allocation:
    try:
        entries = json.loads(Path(os.environ["IACP_TERRAFORM_ALLOCATIONS_FILE"]).read_text())
        return Allocation.model_validate(entries[f"{instance.tenant_id}/{instance.name}"])
    except (KeyError, OSError, ValueError, TypeError):
        raise PlanError("AUTHORIZED_CONFIGURATION_REQUIRED") from None


def instance_executor() -> str:
    mode = os.environ.get("IACP_INSTANCE_EXECUTOR", "simulated")
    if mode not in {"simulated", "terraform"}:
        raise PlanError("EXECUTOR_UNAVAILABLE")
    return mode
