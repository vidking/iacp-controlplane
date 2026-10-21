from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Instance, TerraformAttempt, TerraformExecution
from ..schemas import ErrorResponse
from ..security import Principal, authorize, authorize_write
from ..services.instances import Conflict
from ..services.jobs import QueueUnavailable
from ..terraform.actions import cancel, retry
from .dependencies import get_plan_publisher

router = APIRouter(prefix="/v1/instances", tags=["terraform"], responses={
    401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
    409: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})


class AttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    number: int
    status: str
    actor: str
    error_code: str | None
    plan_sha256: str | None
    created_at: datetime
    finished_at: datetime | None


class ExecutionResponse(BaseModel):
    instance_id: str
    job_id: str
    operation_id: str
    status: Literal["queued", "running", "planned", "failed", "cancelled"]
    attempt: int
    state_key: str
    source_revision: str | None
    plan_sha256: str | None
    error_code: str | None
    cancel_requested: bool
    approval_required: Literal[True] = True
    apply_enabled: Literal[False] = False
    attempts: list[AttemptResponse]


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_attempt: int = Field(ge=1)


class PlanQueued(BaseModel):
    instance_id: str
    operation_id: str
    job_id: str
    attempt: int
    status: Literal["queued"]


def owned_execution(instance_id, principal, session):
    execution = session.scalar(select(TerraformExecution).join(Instance).where(
        TerraformExecution.instance_id == instance_id, Instance.tenant_id == principal.tenant_id))
    if execution is None:
        raise HTTPException(status_code=404, detail="Terraform execution not found")
    return execution


def response(execution, session):
    attempts = session.scalars(select(TerraformAttempt).where(TerraformAttempt.instance_id == execution.instance_id)
                              .order_by(TerraformAttempt.number))
    return ExecutionResponse(**{key: getattr(execution, key) for key in (
        "instance_id", "job_id", "operation_id", "status", "attempt", "state_key", "source_revision",
        "plan_sha256", "error_code", "cancel_requested")}, attempts=[AttemptResponse.model_validate(row) for row in attempts])


@router.get("/{instance_id}/terraform", response_model=ExecutionResponse, operation_id="getTerraformExecution")
def get_execution(instance_id: str, principal: Annotated[Principal, Depends(authorize)],
                  session: Annotated[Session, Depends(get_session)]):
    return response(owned_execution(instance_id, principal, session), session)


@router.post("/{instance_id}/terraform/cancel", response_model=ExecutionResponse, operation_id="cancelTerraformPlan")
def cancel_execution(instance_id: str, principal: Annotated[Principal, Depends(authorize_write)],
                     session: Annotated[Session, Depends(get_session)]):
    execution = owned_execution(instance_id, principal, session)
    cancel(session, execution, principal.subject)
    return response(execution, session)


@router.post("/{instance_id}/terraform/retry", status_code=202, response_model=PlanQueued,
             operation_id="retryTerraformPlan", description="Reintenta solo planificación. Conserva instancia, asignación y state; nunca aplica un plan.")
def retry_execution(instance_id: str, request: RetryRequest,
                    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")],
                    principal: Annotated[Principal, Depends(authorize_write)],
                    session: Annotated[Session, Depends(get_session)],
                    publish=Depends(get_plan_publisher)):
    try:
        return retry(session, owned_execution(instance_id, principal, session), request.expected_attempt, key, principal.subject, publish)
    except Conflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except QueueUnavailable:
        raise HTTPException(status_code=503, detail="Queue unavailable; repeat with the same retry key") from None
