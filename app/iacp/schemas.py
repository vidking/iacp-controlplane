from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from .api.presentation import public_code, public_executor, public_message, public_result
from .commercial_contract import OrderContext

JobStatus = Literal["queued", "running", "completed", "failed"]
OperationStatus = Literal["queued", "running", "completed", "failed", "uncertain"]


class HealthResponse(BaseModel):
    status: Literal["live", "ready"]


class ErrorResponse(BaseModel):
    detail: str


class JobAccepted(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str = Field(validation_alias="id")
    operation: str
    status: JobStatus
    message: str
    created_at: datetime
    updated_at: datetime

    _message = field_validator("message")(public_message)


class CommercialContext(BaseModel):
    """Opaque CRM references; never evidence of authorization or payment."""
    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    order_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    product_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class RootAccess(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    username: Literal['root']
    password: SecretStr = Field(min_length=16, max_length=128, repr=False,
        description='Solo entrada HTTPS. Contraseña única por VM. En laboratorio el plan/state y sus versiones históricas pueden conservarla; nunca se devuelve por API.')

    @field_validator('password')
    @classmethod
    def usable_password(cls, value):
        if any(ord(c) < 32 or ord(c) > 126 for c in value.get_secret_value()):
            raise ValueError('Printable ASCII required')
        return value


class AccessProgress(BaseModel):
    username: Literal['root'] = 'root'
    status: Literal['pending', 'verifying', 'completed', 'failed', 'uncertain'] = 'pending'
    cloud_init_complete: bool | None = None
    ssh_authenticated: bool | None = None
    observed_at: str | None = None
    error_code: str | None = None


class InstanceAccepted(JobAccepted):
    instance_id: str = Field(description="UUID estable de la instancia IACP; nunca un VMID de Proxmox.")
    operation_id: str = Field(description="UUID de la intención asíncrona; consultar GET /v1/operations/{operation_id}.")
    executor: Literal["simulated", "provisioning"] = Field(default="simulated",
        description="Modo de servicio de IACP; no permite seleccionar una implementación.")
    provisioning_service: Literal["provisioning"] = "provisioning"
    _executor = field_validator("executor", mode="before")(public_executor)


class InstanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    instance_id: str = Field(validation_alias="id")
    operation_id: str
    job_id: str
    tenant_id: str
    name: str
    image: str
    network_profile: str | None = None
    node_preference: str | None = None
    commercial_context: CommercialContext | OrderContext | None = None
    start_on_create: bool = False
    access: AccessProgress | None = None
    network_mode: Literal["static", "dhcp"] = "static"
    ip_addresses: list[str] = Field(default_factory=list)
    network_observation: dict | None = None
    vcpus: int
    memory_mb: int
    disk_gb: int
    status: Literal["pending", "provisioning", "ready", "failed", "deleting", "deleted"]
    executor: Literal["simulated", "provisioning"]
    provisioning_service: Literal["provisioning"] = "provisioning"
    infrastructure_kind: Literal["simulated", "unprovisioned", "kvm"]
    vmid: int | None
    node: str | None
    result: dict | None
    created_at: datetime
    updated_at: datetime

    _result = field_validator("result", mode="before")(public_result)
    _executor = field_validator("executor", mode="before")(public_executor)


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: OperationStatus
    code: str
    actor: str
    created_at: datetime

    _neutral_code = field_validator("code", "actor")(public_code)


class OperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    operation_id: str = Field(validation_alias="id")
    instance_id: str
    kind: str
    status: OperationStatus
    requested_by: str
    error_code: str | None
    error_message: str | None = None
    normalized_status: OperationStatus
    recovery_required: bool = False
    provisioning: dict | None = None
    created_at: datetime
    updated_at: datetime
    events: list[EventResponse]
    power: "PowerProgress | None" = None
    deletion: "DeletionProgress | None" = None
    network: dict | None = None
    initialization: dict | None = None
    fulfillment: dict | None = Field(default=None,
        description="Para start_on_create: estado conjunto de aprovisionamiento, start e inicialización; completed exige sus comprobaciones, no readiness de una aplicación.")

    _neutral_error = field_validator("error_code")(public_code)


class PowerRequest(BaseModel):
    """Transición autorizada sin parámetros adicionales del cliente."""
    model_config = ConfigDict(extra="forbid", strict=True)


class PowerAccepted(BaseModel):
    instance_id: str
    operation_id: str
    job_id: str
    status: OperationStatus


class DeletionAccepted(PowerAccepted):
    """Operation tracking for a disposable deletion plan or command."""


class NetworkAccepted(PowerAccepted):
    """Read-only infrastructure observation, tracked asynchronously."""


class GuestInitializationRecoveryRequest(BaseModel):
    """Identidad exacta y modo de recuperación de una inicialización."""
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    job_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    mode: Literal["cancel-queued", "verify-consumed"] = "cancel-queued"


class GuestInitializationRecoveryAccepted(BaseModel):
    instance_id: str
    operation_id: str
    job_id: str
    status: Literal["failed", "completed"]
    error_code: str | None = None
    event_code: str
    mode: Literal["cancel-queued", "verify-consumed"] = "cancel-queued"
    changed: bool
    already_completed: bool = False


class PowerState(BaseModel):
    instance_id: str
    power_state: Literal["running", "stopped"]
    observed_at: datetime
    os_boot: Literal["unknown"] = "unknown"
    guest_agent: Literal["unknown"] = "unknown"
    cloud_init: Literal["unknown"] = "unknown"
    accessible: Literal["unknown"] = "unknown"
    guest_ready: None = None
    mutations_available: bool
    blocked_reason: str | None = None


class PowerProgress(BaseModel):
    action: Literal["start", "shutdown", "reboot"]
    status: OperationStatus
    phase: str
    power_state: Literal["running", "stopped"] | None
    guest_ready: None = None
    recovery_required: bool
    started_at: datetime | None
    submitted_at: datetime | None
    finished_at: datetime | None


class DeletionProgress(BaseModel):
    status: Literal["queued", "running", "planned", "approved", "uncertain", "completed", "failed"]
    phase: str
    plan_sha256: str | None
    approved_by: str | None
    expires_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    recovery_required: bool


OperationResponse.model_rebuild()
