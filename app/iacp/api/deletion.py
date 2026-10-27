"""A reviewed saved destroy plan precedes DELETE; provider identity stays server-side."""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Body, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_session
from ..schemas import DeletionAccepted, ErrorResponse, PowerRequest
from ..security import Principal, authorize_administration, authorize_approval, authorize_write
from ..services.instances import Conflict
from ..services.jobs import QueueUnavailable
from ..services.provisioning import ProvisioningService

deletion = ProvisioningService.deletion
from .dependencies import get_deletion_publisher
from .power import owned

router = APIRouter(prefix="/v1/instances/{instance_id}", tags=["disposable-deletion"], responses={
    401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
    409: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
DeleteKey = str
Database = Annotated[Session, Depends(get_session)]
Publisher = Annotated[object, Depends(get_deletion_publisher)]


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(pattern=r"^[a-f0-9-]{36}$", examples=["00000000-0000-4000-8000-000000000002"])
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", description="Digest exacto del plan DELETE revisado; no acepta VMID, nodo ni force.")


class DeleteApprovalRequest(DeleteRequest):
    valid_for_seconds: int = Field(default=900, ge=60, le=3600)


def guarded(call):
    try:
        return call()
    except Conflict as failure:
        raise HTTPException(409, str(failure)) from None
    except QueueUnavailable:
        raise HTTPException(503, "DELETE_DISPATCH_UNCONFIRMED; repeat the SAME key") from None


@router.post("/provisioning/deletion-plan", response_model=DeletionAccepted, status_code=202,
             operation_id="planDisposableDeletion", dependencies=[Depends(authorize_administration)], summary="Preparar plan de eliminación sin borrar",
             description="Operator del tenant. Solo UUID en allowlist, VMID >=30000 y KVM propia con Apply completado. "
                         "Reserva la instancia frente a Lifecycle; genera un plan destroy guardado, sin Apply. "
                         "Consultar operation.deletion en GET /v1/operations/{operation_id}. Conservar esta misma Idempotency-Key para DELETE.")
def plan(instance_id: str, key: Key, session: Database, publish: Publisher,
         principal: Annotated[Principal, Depends(authorize_write)], body: PowerRequest | None = None):
    instance = owned(session, principal, instance_id)
    return guarded(lambda: deletion.plan_request(session, instance, principal, key, publish))


@router.post("/provisioning/deletion-approval", response_model=DeletionAccepted, status_code=201,
             operation_id="approveDisposableDeletion", dependencies=[Depends(authorize_administration)], summary="Aprobar el plan DELETE revisado",
             description="Approver del mismo tenant, sujeto distinto del solicitante. Liga aprobación de un uso "
                         "al operation_id y SHA-256 del plan, con caducidad. No ejecuta infraestructura.")
def approve(instance_id: str, body: DeleteApprovalRequest, key: Key, session: Database,
            principal: Annotated[Principal, Depends(authorize_approval)]):
    instance = owned(session, principal, instance_id)
    return guarded(lambda: deletion.approve(session, instance, principal, body.model_dump(), key))


@router.delete("", response_model=DeletionAccepted, status_code=202, operation_id="deleteDisposableInstance",
               summary="Eliminar una KVM descartable con plan aprobado",
               description="Operator original y una Idempotency-Key opcional. Si se omite, IACP genera una intención y la devuelve en la respuesta. Requiere habilitación explícita, "
                           "allowlist y aprobación independiente vigente. Para una VM nueva bajo política de autoservicio, "
                           "el cuerpo es opcional para el autoservicio: prepara, valida y ejecuta el plan por delegación auditada. "
                           "IaC Worker aplica únicamente el plan guardado. "
                           "Tras empezar una mutación, un replay solo reconcilia por lectura; nunca repite Apply. "
                           "Completed exige ausencia en state y Proxmox. Conserva inventario y auditoría con tombstone deleted. "
                           "VM 104 y VM 106 quedan prohibidas. No invoca DELETE nativo ni borra volúmenes Docker.")
def delete(instance_id: str, response: Response, session: Database, publish: Publisher,
           principal: Annotated[Principal, Depends(authorize_write)],
           key: DeleteKey = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
           body: Annotated[DeleteRequest | PowerRequest | None, Body(openapi_examples={
               "self_service":{"summary":"VM nueva bajo política vigente; plan y delegación auditados","value":{}},
               "reviewed_plan":{"summary":"Contrato histórico; sustituir por IDs/digest reales revisados","value":{
                   "operation_id":"00000000-0000-4000-8000-000000000002","plan_sha256":"a"*64}}})] = None):
    from .idempotency import resolve_key
    key = resolve_key(key, 'delete', response)
    instance = owned(session, principal, instance_id)
    if not isinstance(body, DeleteRequest):
        return guarded(lambda: deletion.plan_request(session, instance, principal, key, publish, autonomous=True))
    return guarded(lambda: deletion.dispatch(session, instance, principal, body.model_dump(), key, publish))


@router.post("/provisioning/deletions/{operation_id}/cancel", response_model=DeletionAccepted,
             operation_id="cancelDisposableDeletion", dependencies=[Depends(authorize_administration)], summary="Cancelar antes de consumir la autorización",
             description="Operator original. Cancela únicamente si started_at sigue vacío, con lock exclusivo. "
                         "No ejecuta infraestructura; disponible durante el drenado. No libera una mutación incierta.")
def cancel(instance_id: str, operation_id: str, session: Database, publish: Publisher,
           principal: Annotated[Principal, Depends(authorize_write)], body: PowerRequest | None = None):
    instance = owned(session, principal, instance_id)
    return guarded(lambda: deletion.recover_or_cancel(session, instance, principal, operation_id, publish, cancel=True))


@router.post("/provisioning/deletions/{operation_id}/recover", response_model=DeletionAccepted, status_code=202,
             operation_id="recoverDisposableDeletion", dependencies=[Depends(authorize_administration)], summary="Reconciliar eliminación consumida solo por lectura",
             description="Operator original. Exige started_at persistido y reserva running/uncertain. "
                         "Publica el mismo identificador exclusivamente en modo reconcile. Disponible con admisión/DELETE OFF; "
                         "nunca ejecuta init, plan ni Apply ni autoriza un segundo intento de borrado.")
def recover(instance_id: str, operation_id: str, session: Database, publish: Publisher,
            principal: Annotated[Principal, Depends(authorize_write)], body: PowerRequest | None = None):
    instance = owned(session, principal, instance_id)
    return guarded(lambda: deletion.recover_or_cancel(session, instance, principal, operation_id, publish))
