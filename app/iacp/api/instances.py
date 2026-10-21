from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_session
from ..models import Instance, Operation
from ..schemas import ErrorResponse, InstanceAccepted, InstanceCreate, InstanceResponse
from ..security import Principal, authorize, authorize_write
from ..services.instances import Conflict, create_instance
from ..services.jobs import Publisher, QueueUnavailable
from .dependencies import get_instance_publisher

router = APIRouter(
    prefix="/v1/instances", tags=["instances"],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


@router.post(
    "", status_code=202, response_model=InstanceAccepted, operation_id="createInstance",
    responses={409: {"model": ErrorResponse}},
    description=("Crea instancia, operación y job de forma atómica. El servidor selecciona simulación o planificación Terraform; apply permanece deshabilitado. "
                 "Idempotency-Key es obligatorio y se conserva por tenant; repetirlo con otro cuerpo devuelve 409. "
                 "Una repetición idéntica devuelve los mismos IDs y no vuelve a publicar el job, incluso si falló."),
)
def post_instance(
    spec: InstanceCreate,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")],
    principal: Annotated[Principal, Depends(authorize_write)],
    session: Annotated[Session, Depends(get_session)],
    publish: Annotated[Publisher, Depends(get_instance_publisher)],
) -> InstanceAccepted:
    try:
        return create_instance(session, principal, spec, key, publish)
    except Conflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except QueueUnavailable:
        raise HTTPException(status_code=503, detail="Queue unavailable; repeat with the same Idempotency-Key to retrieve the operation") from None


@router.get("", response_model=list[InstanceResponse], operation_id="listInstances")
def list_instances(
    principal: Annotated[Principal, Depends(authorize)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[InstanceResponse]:
    rows = session.scalars(select(Instance).options(selectinload(Instance.creation).selectinload(Operation.job))
                           .where(Instance.tenant_id == principal.tenant_id)
                           .order_by(Instance.created_at, Instance.id).limit(limit).offset(offset))
    return [InstanceResponse.model_validate(row) for row in rows]


@router.get("/{instance_id}", response_model=InstanceResponse, operation_id="getInstance",
            responses={404: {"model": ErrorResponse}})
def get_instance(instance_id: str, principal: Annotated[Principal, Depends(authorize)],
                 session: Annotated[Session, Depends(get_session)]) -> InstanceResponse:
    row = session.scalar(select(Instance).where(Instance.id == instance_id, Instance.tenant_id == principal.tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    return InstanceResponse.model_validate(row)
