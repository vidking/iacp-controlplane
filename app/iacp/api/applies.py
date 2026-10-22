"""Contrato de aprobación, ejecución y conciliación del plan guardado."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import TerraformApply, TerraformExecution
from ..schemas import ErrorResponse
from ..security import Principal, authorize, authorize_approval, authorize_write
from ..services.instances import Conflict
from ..services.jobs import QueueUnavailable
from ..terraform.apply import approve, cancel_apply, dispatch, recover_apply, resolve_absence
from .dependencies import get_apply_publisher
from .terraform import owned_execution

router = APIRouter(prefix="/v1/instances/{instance_id}/terraform", tags=["terraform-apply"], responses={
    401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
    409: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    attempt: int = Field(ge=1)
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    valid_for_seconds: int = Field(default=900, ge=60, le=3600)


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approval_id: str = Field(pattern=r"^[a-f0-9-]{36}$")


class AbsenceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    observed_at: datetime = Field(strict=False)
    evidence_ref: str = Field(min_length=1, max_length=512)

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("La evidencia requiere fecha con zona horaria")
        return value


class EmptyStateEvidence(AbsenceEvidence):
    bucket: str = Field(min_length=1, max_length=63)
    key: str = Field(min_length=1, max_length=512)
    version_id: str = Field(min_length=1, max_length=1024)
    lineage: str = Field(min_length=1, max_length=128)
    serial: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resource_count: Literal[0]


class ProxmoxAbsenceEvidence(AbsenceEvidence):
    vmid: int = Field(ge=100)
    node: str = Field(min_length=1, max_length=128)
    scope: Literal["cluster"]
    absent: Literal[True]


class AbsenceResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    attempt: int = Field(ge=1)
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: EmptyStateEvidence
    proxmox: ProxmoxAbsenceEvidence
    reason: str = Field(min_length=1, max_length=2000)


class ApplyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    approval_id: str = Field(validation_alias="id")
    instance_id: str
    job_id: str | None
    attempt: int
    plan_sha256: str
    approved_by: str
    requested_by: str | None
    expires_at: datetime
    status: Literal["approved", "queued", "running", "completed", "failed", "uncertain", "cancelled"]
    error_code: str | None
    result: dict | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @field_validator("expires_at", "created_at", "started_at", "finished_at")
    @classmethod
    def utc_dates(cls, value):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def conflicting(call):
    try:
        return call()
    except Conflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except QueueUnavailable:
        raise HTTPException(status_code=503, detail="DISPATCH_UNCONFIRMED") from None


@router.post("/approvals", response_model=ApplyResponse, status_code=201, operation_id="approveTerraformPlan",
             description="Aprobación de un solo uso, ligada al intento y SHA-256. Exige rol approver y sujeto distinto del solicitante.")
def approve_plan(instance_id: str, body: ApprovalRequest, key: Key,
                 principal: Annotated[Principal, Depends(authorize_approval)], session: Annotated[Session, Depends(get_session)]):
    execution = owned_execution(instance_id, principal, session)
    return conflicting(lambda: approve(session, execution, body.model_dump(), key, principal))


@router.post("/apply", response_model=ApplyResponse, status_code=202, operation_id="applyApprovedTerraformPlan",
             description="Encola exclusivamente el plan aprobado. Requiere habilitación del entorno; repetir clave y aprobación no republica.")
def apply_plan(instance_id: str, body: ApplyRequest, key: Key,
               principal: Annotated[Principal, Depends(authorize_write)], session: Annotated[Session, Depends(get_session)],
               publish=Depends(get_apply_publisher)):
    execution = owned_execution(instance_id, principal, session)
    return conflicting(lambda: dispatch(session, execution, body.approval_id, key, principal, publish))


@router.get("/applies", response_model=list[ApplyResponse], operation_id="listTerraformApplies")
def list_applies(instance_id: str, principal: Annotated[Principal, Depends(authorize)],
                 session: Annotated[Session, Depends(get_session)]):
    owned_execution(instance_id, principal, session)
    return session.scalars(select(TerraformApply).where(TerraformApply.instance_id == instance_id).order_by(TerraformApply.created_at)).all()


@router.post("/applies/{approval_id}/cancel", response_model=ApplyResponse, operation_id="cancelTerraformApply")
def cancel(instance_id: str, approval_id: str, principal: Annotated[Principal, Depends(authorize_write)],
           session: Annotated[Session, Depends(get_session)]):
    execution = owned_execution(instance_id, principal, session)
    return conflicting(lambda: cancel_apply(session, execution, approval_id, principal))


@router.post("/applies/{approval_id}/recover", response_model=ApplyResponse, status_code=202, operation_id="recoverTerraformApply",
             description="Concilia queued/running/uncertain exclusivamente por observación. Cerca entregas apply pendientes y nunca ejecuta Terraform Apply.")
def recover(instance_id: str, approval_id: str, principal: Annotated[Principal, Depends(authorize_write)],
            session: Annotated[Session, Depends(get_session)], publish=Depends(get_apply_publisher)):
    execution = owned_execution(instance_id, principal, session)
    return conflicting(lambda: recover_apply(session, execution, approval_id, principal, publish))


@router.post("/applies/{approval_id}/resolve-absence", response_model=ApplyResponse, operation_id="resolveTerraformAbsence",
             description="Atestación de un approver independiente con evidencia de state vacío y ausencia en el clúster Proxmox. Cierra en failed; no consulta ni modifica infraestructura.")
def resolve(instance_id: str, approval_id: str, body: AbsenceResolutionRequest, key: Key,
            principal: Annotated[Principal, Depends(authorize_approval)], session: Annotated[Session, Depends(get_session)]):
    execution = owned_execution(instance_id, principal, session)
    return conflicting(lambda: resolve_absence(session, execution, approval_id, body.model_dump(mode="json"), key, principal))
