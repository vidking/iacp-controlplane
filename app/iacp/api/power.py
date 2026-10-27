from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..lifecycle.ownership import instance_ownership, mutation_revision
from ..lifecycle.proxmox import PowerError
from ..lifecycle.service import managed_target, request_power
from ..models import Instance
from ..schemas import ErrorResponse, PowerAccepted, PowerRequest, PowerState
from ..security import Principal, authorize, authorize_write
from ..services.instances import Conflict
from ..services.jobs import QueueUnavailable
from .dependencies import get_power_client, get_power_publisher

router = APIRouter(prefix="/v1/instances/{instance_id}/power", tags=["kvm-lifecycle"], responses={
    401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
    409: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})

PrincipalRead = Annotated[Principal, Depends(authorize)]
PrincipalWrite = Annotated[Principal, Depends(authorize_write)]
Database = Annotated[Session, Depends(get_session)]
RequestKey = str
ClientFactory = Annotated[object, Depends(get_power_client)]
Publisher = Annotated[object, Depends(get_power_publisher)]


def owned(session, principal, instance_id):
    row = session.scalar(select(Instance).where(Instance.id == instance_id, Instance.tenant_id == principal.tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    return row


@router.get("", response_model=PowerState, operation_id="getInstancePower",
            description="Consulta Proxmox para una KVM administrada del tenant. Running no demuestra arranque del SO, Guest Agent, Cloud-Init ni accesibilidad; guest_ready permanece null.")
def get_power(instance_id: str, principal: PrincipalRead, session: Database, client_factory: ClientFactory):
    instance = owned(session, principal, instance_id)
    try:
        target, approval = managed_target(session, instance)
        client = client_factory()
        client.permissions(target)
        observed = client.observe(target)
        reason = None
        try:
            mutation_revision()
            instance_ownership(session, instance, approval)
            from ..terraform.deletion import active_deletion
            if active_deletion(session, instance.id):
                reason = "DELETE_OPERATION_ACTIVE"
        except PowerError as failure:
            reason = failure.code
        return PowerState(instance_id=instance.id, **observed, mutations_available=reason is None, blocked_reason=reason)
    except Conflict as failure:
        raise HTTPException(status_code=409, detail=str(failure)) from None
    except PowerError as failure:
        raise HTTPException(status_code=503, detail=failure.code) from None


def accept(instance_id, principal, session, key, action, publish, client_factory, response=None):
    headers = {"Idempotency-Key": key}
    instance = owned(session, principal, instance_id)
    try:
        return request_power(session, instance, principal, action, key, publish, client_factory)
    except Conflict as failure:
        raise HTTPException(status_code=409, detail=str(failure), headers=headers) from None
    except PowerError as failure:
        raise HTTPException(status_code=503, detail=failure.code, headers=headers) from None
    except QueueUnavailable:
        raise HTTPException(status_code=503, detail="POWER_DISPATCH_UNCONFIRMED; repeat the same Idempotency-Key",
                            headers=headers) from None


DESCRIPTION = ("Operación asíncrona para una KVM administrada del tenant. Solo operator; Idempotency-Key opcional. "
               "Sin parámetros de VMID/nodo ni fuerza. Requiere contrato de propiedad started validado; de otro modo 409. "
               "202 incluye operation_id para GET /v1/operations/{operation_id}. Repetir la clave conserva IDs y reanuda "
               "la observación tras un envío; si se omite, IACP genera una clave y la devuelve en el header de respuesta. "
               "La consola no solicita cuerpo ni clave manual. Nunca repite una mutación consumida. Un resultado ambiguo permanece uncertain.")


@router.post("/start", status_code=202, response_model=PowerAccepted, operation_id="startInstancePower", description=DESCRIPTION)
def start(instance_id: str, response: Response, principal: PrincipalWrite, session: Database,
          publish: Publisher, client_factory: ClientFactory,
          key: RequestKey = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
          body: PowerRequest | None = None):
    from .idempotency import resolve_key
    key = resolve_key(key, 'start', response)
    return accept(instance_id, principal, session, key, "start", publish, client_factory, response)


@router.post("/shutdown", status_code=202, response_model=PowerAccepted, operation_id="shutdownInstancePower",
             description=DESCRIPTION + " Apagado ordenado, timeout ACPI 60 s; forceStop=0 sin fallback forzado.")
def shutdown(instance_id: str, response: Response, principal: PrincipalWrite, session: Database,
             publish: Publisher, client_factory: ClientFactory,
             key: RequestKey = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
             body: PowerRequest | None = None):
    from .idempotency import resolve_key
    key = resolve_key(key, 'shutdown', response)
    return accept(instance_id, principal, session, key, "shutdown", publish, client_factory, response)


@router.post("/reboot", status_code=202, response_model=PowerAccepted, operation_id="rebootInstancePower",
             description=DESCRIPTION + " Requiere VM running y tarea de reboot confirmada; running por sí solo no demuestra reboot.")
def reboot(instance_id: str, response: Response, principal: PrincipalWrite, session: Database,
           publish: Publisher, client_factory: ClientFactory,
           key: RequestKey = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
           body: PowerRequest | None = None):
    from .idempotency import resolve_key
    key = resolve_key(key, 'reboot', response)
    return accept(instance_id, principal, session, key, "reboot", publish, client_factory, response)
