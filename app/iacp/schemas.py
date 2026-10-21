from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal["queued", "running", "completed", "failed"]


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


class InstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
    image: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    vcpus: int = Field(default=2, ge=1, le=64)
    memory_mb: int = Field(default=2048, ge=512, le=262144, multiple_of=256)
    disk_gb: int = Field(default=20, ge=8, le=4096)


class InstanceAccepted(JobAccepted):
    instance_id: str
    operation_id: str
    executor: Literal["simulated"] = "simulated"


class InstanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    instance_id: str = Field(validation_alias="id")
    operation_id: str
    job_id: str
    tenant_id: str
    name: str
    image: str
    vcpus: int
    memory_mb: int
    disk_gb: int
    status: Literal["pending", "provisioning", "ready", "failed"]
    executor: Literal["simulated"]
    vmid: int | None
    node: str | None
    result: dict | None
    created_at: datetime
    updated_at: datetime


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: JobStatus
    code: str
    actor: str
    created_at: datetime


class OperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    operation_id: str = Field(validation_alias="id")
    instance_id: str
    kind: str
    status: JobStatus
    requested_by: str
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    events: list[EventResponse]
