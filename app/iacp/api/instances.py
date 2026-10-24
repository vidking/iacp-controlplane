from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import select, func
from sqlalchemy.orm import Session, selectinload

from ..db import get_session
from ..models import Instance, Operation
from ..schemas import ErrorResponse, InstanceAccepted, InstanceCreate, InstanceResponse, NetworkAccepted, PowerRequest
from ..security import Principal, authorize, authorize_write
from ..services.instances import Conflict, create_instance
from ..services.jobs import Publisher, QueueUnavailable
from .dependencies import get_guest_initialization_publisher, get_instance_publisher

router = APIRouter(
    prefix="/v1/instances", tags=["instances"],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


@router.post(
    "", status_code=202, response_model=InstanceAccepted, operation_id="createInstance",
    responses={409: {"model": ErrorResponse}},
    description=("Crea instancia, operación y job de forma atómica. El IaC Worker usa el perfil autorizado; una política aprobada permite completar el plan por delegación. Sin política se conserva la revisión individual y habilitación explícita. "
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
    response: Response,
    principal: Annotated[Principal, Depends(authorize)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Literal["pending", "provisioning", "ready", "failed", "deleting", "deleted"] | None = None,
    network_profile: Annotated[str | None, Query(max_length=63)] = None,
    name: Annotated[str | None, Query(max_length=63, description="Nombre exacto; único por tenant")] = None,
    node: Annotated[str | None, Query(max_length=128)] = None,
) -> list[InstanceResponse]:
    filters=[Instance.tenant_id == principal.tenant_id]
    for column,value in [(Instance.status,status),(Instance.network_profile,network_profile),(Instance.name,name),(Instance.node,node)]:
        if value is not None:
            filters.append(column==value)
    response.headers['X-Total-Count']=str(session.scalar(select(func.count()).select_from(Instance).where(*filters)))
    rows = session.scalars(select(Instance).options(selectinload(Instance.creation).selectinload(Operation.job))
                           .where(*filters)
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


@router.post('/{instance_id}/network/discover',status_code=202,response_model=NetworkAccepted,operation_id='discoverInstanceNetwork',
             responses={404:{'model':ErrorResponse},409:{'model':ErrorResponse}},
             description='Consulta asíncrona del Guest Agent con MAC e identidad verificadas. No reserva IPAM ni modifica state. Una IP pendiente no falla la creación.')
def discover_network(instance_id: str,principal: Annotated[Principal,Depends(authorize_write)],
                     key: Annotated[str,Header(alias='Idempotency-Key',min_length=1,max_length=128,pattern=r'^[A-Za-z0-9._:-]+$')],
                     session: Annotated[Session,Depends(get_session)],body: PowerRequest | None = None):
    from .power import owned
    from .dependencies import get_network_publisher
    from ..services.network import request_discovery
    row=owned(session,principal,instance_id)
    try:return request_discovery(session,row,principal,key,get_network_publisher())
    except Conflict as error:raise HTTPException(409,str(error)) from None
    except QueueUnavailable:raise HTTPException(503,'NETWORK_DISPATCH_UNCONFIRMED') from None


@router.post('/{instance_id}/network/initialize',status_code=202,response_model=NetworkAccepted,operation_id='initializeInstanceNetwork',
    responses={404:{'model':ErrorResponse},409:{'model':ErrorResponse}},
    description='Inicialización fija y única de identidad Ubuntu 24.04 para una clon nueva de laboratorio. Requiere perfil/política revisados, propiedad y Lifecycle habilitado. '
    'Sin comandos, archivos ni parámetros del cliente. Tras consumir el envío solo observa evidencia. Start la solicita automáticamente; esta ruta recupera una entrega no confirmada con la misma clave.')
def initialize_network(instance_id: str,principal: Annotated[Principal,Depends(authorize_write)],
    key: Annotated[str,Header(alias='Idempotency-Key',min_length=1,max_length=128,pattern=r'^[A-Za-z0-9._:-]+$')],
    session: Annotated[Session,Depends(get_session)],publish=Depends(get_guest_initialization_publisher),
    body: PowerRequest | None = None):
    from .power import owned
    from ..services.guest_identity import request_initialization
    row=owned(session,principal,instance_id)
    try:return request_initialization(session,row,principal,key,publish)
    except Conflict as error:raise HTTPException(409,str(error)) from None
    except QueueUnavailable:raise HTTPException(503,'GUEST_INITIALIZATION_DISPATCH_UNCONFIRMED; repeat the same key') from None
