from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import DeletionExecution, Operation, OperationEvent, PowerExecution
from ..schemas import DeletionProgress, ErrorResponse, EventResponse, OperationResponse, PowerProgress
from ..security import Principal, authorize

router = APIRouter(prefix="/v1/operations", tags=["operations"], responses={
    401: {"model": ErrorResponse}, 403: {"model": ErrorResponse},
    404: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})


@router.get("/{operation_id}", response_model=OperationResponse, operation_id="getOperation")
def get_operation(operation_id: str, principal: Annotated[Principal, Depends(authorize)],
                  session: Annotated[Session, Depends(get_session)]) -> OperationResponse:
    row = session.scalar(select(Operation).where(Operation.id == operation_id, Operation.tenant_id == principal.tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    events = session.scalars(select(OperationEvent).where(OperationEvent.operation_id == row.id).order_by(OperationEvent.id))
    power = session.get(PowerExecution, row.id)
    deletion = session.get(DeletionExecution, row.id)
    return OperationResponse(
        id=row.id, instance_id=row.instance_id, kind=row.kind, status=row.status,
        requested_by=row.requested_by, error_code=row.error_code, created_at=row.created_at, updated_at=row.updated_at,
        events=[EventResponse.model_validate(item) for item in events],
        deletion=(DeletionProgress(**{key: getattr(deletion, key) for key in (
            "status", "phase", "plan_sha256", "approved_by", "expires_at", "started_at", "finished_at")},
            recovery_required=deletion.status == "uncertain") if deletion else None),
        power=(PowerProgress(action=power.action, status=power.status, phase=power.phase,
                             power_state=(power.result or {}).get("power_state"),
                             recovery_required=power.status == "uncertain", started_at=power.started_at,
                             submitted_at=power.submitted_at, finished_at=power.finished_at) if power else None),
    )
