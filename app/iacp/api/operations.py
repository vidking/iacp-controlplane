from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import DeletionExecution, GuestInitialization, Instance, NetworkDiscovery, Operation, OperationEvent, PowerExecution, TerraformApply, TerraformExecution
from ..schemas import DeletionProgress, ErrorResponse, EventResponse, OperationResponse, PowerProgress
from ..security import Principal, authorize
from ..services.fulfillment import progress

router = APIRouter(prefix="/v1/operations", tags=["operations"], responses={
    401: {"model": ErrorResponse}, 403: {"model": ErrorResponse},
    404: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})


@router.get("", response_model=list[OperationResponse], operation_id="listOperations",
    description="Historial del tenant con paginación estable. Filtrar por instance_id permite auditar creación, Lifecycle, red y DELETE.")
def list_operations(principal: Annotated[Principal, Depends(authorize)], session: Annotated[Session, Depends(get_session)],
    instance_id: str | None = None, status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0):
    query=select(Operation).where(Operation.tenant_id == principal.tenant_id)
    if instance_id is not None:
        query=query.where(Operation.instance_id == instance_id)
    if status is not None:
        query=query.where(Operation.status == status)
    rows=session.scalars(query.order_by(Operation.created_at,Operation.id).limit(limit).offset(offset)).all()
    return [get_operation(row.id,principal,session) for row in rows]


@router.get("/{operation_id}", response_model=OperationResponse, operation_id="getOperation")
def get_operation(operation_id: str, principal: Annotated[Principal, Depends(authorize)],
                  session: Annotated[Session, Depends(get_session)]) -> OperationResponse:
    row = session.scalar(select(Operation).where(Operation.id == operation_id, Operation.tenant_id == principal.tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    events = session.scalars(select(OperationEvent).where(OperationEvent.operation_id == row.id).order_by(OperationEvent.id))
    power = session.get(PowerExecution, row.id)
    deletion = session.get(DeletionExecution, row.id)
    network = session.get(NetworkDiscovery,row.id)
    initialization=session.get(GuestInitialization,row.id)
    provisioning=None
    normalized=row.status
    if row.kind=='instance.create':
        execution=session.get(TerraformExecution,row.instance_id)
        if execution:
            approval=session.scalar(select(TerraformApply).where(TerraformApply.instance_id==row.instance_id,
                TerraformApply.attempt==execution.attempt))
            phase=approval.status if approval else execution.status
            provisioning={'service':'provisioning','phase':phase,'attempt':execution.attempt,
                'started_at':approval.started_at if approval else None,'finished_at':approval.finished_at if approval else None}
            if approval and approval.status=='uncertain':
                normalized='uncertain'
            elif approval and approval.status in {'queued','running','completed'}:
                # The legacy operation remains failed while readonly recovery is
                # executing. The public status must follow the current run.
                normalized=approval.status
    error_message=None
    if row.error_code:
        error_message=('Conciliación en curso; consulte la misma operación. El error_code conserva el fallo anterior.' if normalized=='running' and row.status=='failed'
            else 'Resultado incierto: reconcilie el mismo UUID; no solicite otra VM ni repita la mutación.' if normalized=='uncertain'
            else 'No se confirmó la entrega; consulte esta operación y continúe con el mismo UUID.' if 'DISPATCH' in row.error_code
            else 'La operación no completó los controles autorizados. Consulte error_code y el historial; conserve UUID e Idempotency-Key.')
    return OperationResponse(
        id=row.id, instance_id=row.instance_id, kind=row.kind, status=row.status,
        requested_by=row.requested_by, error_code=row.error_code, created_at=row.created_at, updated_at=row.updated_at,
        error_message=error_message,normalized_status=normalized,recovery_required=normalized=='uncertain',provisioning=provisioning,
        events=[EventResponse.model_validate(item) for item in events],
        fulfillment=(progress(
            session, session.get(Instance, row.instance_id), normalized) if row.kind == 'instance.create' else None),
        network=network.result if network else None,
        initialization=({'status':initialization.status,'mode':initialization.mode,'program_sha256':initialization.program_sha256,
            'started_at':initialization.started_at,'finished_at':initialization.finished_at,'pid':initialization.pid,
            'result':initialization.result} if initialization else None),
        deletion=(DeletionProgress(**{key: getattr(deletion, key) for key in (
            "status", "phase", "plan_sha256", "approved_by", "expires_at", "started_at", "finished_at")},
            recovery_required=deletion.status == "uncertain") if deletion else None),
        power=(PowerProgress(action=power.action, status=power.status, phase=power.phase,
                             power_state=(power.result or {}).get("power_state"),
                             recovery_required=power.status == "uncertain", started_at=power.started_at,
                             submitted_at=power.submitted_at, finished_at=power.finished_at) if power else None),
    )
