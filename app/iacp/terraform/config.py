import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import model_serializer, BaseModel, ConfigDict, Field, model_validator

SOURCE_REVISION = "48608c364e45be5c70ba043bf245feec2f91d2b3"
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
        self.postcondition = None
        super().__init__(code)

    def diagnostic(self) -> dict:
        """Metadatos operativos permitidos; nunca incluye stdout, stderr ni argumentos."""
        return {key: value for key, value in {
            "phase": self.phase, "command": self.command, "exit_code": self.exit_code,
            "http_status": self.http_status, "state_present": self.state_present,
            "proxmox_present": self.proxmox_present,
            "state_consistency": self.state_consistency,
            "postcondition": self.postcondition,
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


class MachineSettings(StrictModel):
    image: str = Field(min_length=1)
    environment: Literal["lab"]
    allowed_nodes: list[str] = Field(min_length=1)
    backend: Backend
    template_vm_id: int = Field(ge=100)
    node_name: str = Field(min_length=1)
    template_node_name: str | None = None
    pool_id: str | None = None
    clone_datastore_id: str | None = None
    network_bridge: str = Field(min_length=1)
    network_mode: Literal["static", "dhcp"] = "static"
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    cloudinit_datastore_id: str = Field(min_length=1)
    cloudinit_username: str = Field(min_length=1)
    ssh_public_keys: list[str] = Field(min_length=1)
    guest_agent_enabled: bool = False
    started: bool = False
    boot_disk: BootDisk

    @model_validator(mode="after")
    def authorized_machine(self):
        if self.template_vm_id == 104:
            raise ValueError("VM 104 no puede ser plantilla")
        if self.node_name not in self.allowed_nodes or (self.template_node_name and self.template_node_name not in self.allowed_nodes):
            raise ValueError("El nodo debe estar autorizado por el perfil")
        if not self.allowed_nodes or any(not node.strip() for node in self.allowed_nodes):
            raise ValueError("Se requieren nodos autorizados explícitos")
        if not all(key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")) for key in self.ssh_public_keys):
            raise ValueError("Se requieren claves SSH públicas autorizadas")
        return self


class Allocation(MachineSettings):
    # Solo el worker resuelve este snapshot; nunca procede del JSON CRM.
    golden_hardware: dict | None = None

    @model_serializer(mode='wrap')
    def preserve_historical_shape(self, handler):
        values=handler(self)
        if self.golden_hardware is None:values.pop('golden_hardware',None)
        return values

    vm_id: int = Field(ge=100, le=999999999)
    ipv4_cidr: str | None = None
    ipv4_gateway: str | None = None

    @model_validator(mode="after")
    def authorized_target(self):
        from ipaddress import IPv4Interface, IPv4Address
        if self.vm_id == 104 or self.vm_id == self.template_vm_id:
            raise ValueError("El destino debe excluir VM 104 y ser distinto de la plantilla")
        if self.network_mode == "static":
            if self.vlan_id is None:
                raise ValueError("El perfil estático heredado exige VLAN explícita")
            if self.ipv4_cidr is None or self.ipv4_gateway is None:
                raise ValueError("Static exige dirección/gateway IPv4 de IPAM")
            IPv4Interface(self.ipv4_cidr)
            IPv4Address(self.ipv4_gateway)
        elif self.ipv4_cidr is not None or self.ipv4_gateway is not None:
            raise ValueError("DHCP no acepta reservas IPv4 ni gateway estático")
        return self

    def variables(self, instance) -> dict:
        if instance.image != self.image:
            raise PlanError("AUTHORIZED_IMAGE_MISMATCH")
        if instance.disk_gb < self.boot_disk.minimum_size_gb:
            raise PlanError("DISK_SHRINK_FORBIDDEN")
        values = self.model_dump(exclude={"image", "environment", "allowed_nodes", "backend", "golden_hardware"})
        if self.network_mode == "static":
            values.pop("network_mode")  # Preserve sealed historical variables exactly.
        values["boot_disk"]["size_gb"] = instance.disk_gb
        values.update(vm_name=instance.name, cpu_cores=instance.vcpus, memory_mb=instance.memory_mb)
        result = {"environment": self.environment, "allowed_nodes": self.allowed_nodes, "instance": values}
        if self.golden_hardware is not None:
            from .golden_hardware import GoldenHardware
            hardware=GoldenHardware.model_validate(self.golden_hardware)
            if hardware.template_vm_id!=self.template_vm_id or hardware.template_node!=(self.template_node_name or self.node_name):
                raise PlanError('GOLDEN_HARDWARE_BINDING_CHANGED')
            if hardware.disk is not None:
                if values['boot_disk']['interface'] != hardware.disk.interface:
                    raise PlanError('GOLDEN_DISK_INTERFACE_CHANGED')
                if instance.disk_gb < hardware.disk.minimum_size_gb:
                    raise PlanError('DISK_SHRINK_FORBIDDEN')
                # La capacidad es la única mutación técnica que autoriza el
                # plan. Todas las demás opciones se toman de la plantilla;
                # None significa que el atributo no existía y evita defaults
                # silenciosos del provider.
                for key in ('file_format', 'aio', 'cache', 'discard', 'backup',
                            'replicate', 'iothread', 'ssd', 'serial', 'queues'):
                    values['boot_disk'][key] = getattr(hardware.disk, key)
                values['boot_disk']['minimum_size_gb'] = hardware.disk.minimum_size_gb
            result['iacp_golden_hardware']=hardware.model_dump()
        if getattr(instance, 'initial_access_ciphertext', None):
            if getattr(instance,'commercial_revision',None):
                raise PlanError('COMMERCIAL_PASSWORD_IN_TERRAFORM_FORBIDDEN')
            from ..services.access import decrypt_password
            if self.cloudinit_username != 'root' or self.environment != 'lab':
                raise PlanError('ROOT_ACCESS_PROFILE_CHANGED')
            result['iacp_root_password'] = decrypt_password(instance)
        return result


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
