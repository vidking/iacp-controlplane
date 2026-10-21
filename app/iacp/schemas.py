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


class ProvisioningUnavailable(BaseModel):
    code: Literal["IAC_EXECUTOR_NOT_CONFIGURED"] = "IAC_EXECUTOR_NOT_CONFIGURED"
    detail: str = "Aprovisionamiento deshabilitado hasta integrar y validar el IaC Worker"
