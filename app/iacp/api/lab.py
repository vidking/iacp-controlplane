"""Public lab contract; policy administration never executes infrastructure."""
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Instance, LabPolicy, Operation, TerraformExecution, utcnow
from ..schemas import ErrorResponse, PowerAccepted, PowerRequest
from ..security import Principal, authorize, authorize_approval, authorize_write
from ..services import lab
from ..services.instances import Conflict
from ..services.jobs import QueueUnavailable
from ..terraform.locks import admission_lock
from .dependencies import get_apply_publisher, get_guest_initialization_publisher, get_plan_publisher

router = APIRouter(responses={
    401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
    409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
Database = Annotated[Session, Depends(get_session)]
Reader = Annotated[Principal, Depends(authorize)]
Operator = Annotated[Principal, Depends(authorize_write)]
Approver = Annotated[Principal, Depends(authorize_approval)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class PolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    policy_id: str = Field(validation_alias="id")
    tenant_id: str
    status: str
    specification: lab.LabPolicySpec
    policy_sha256: str
    requested_by: str
    approved_by: str | None
    created_at: datetime
    approved_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None
    review_history: list[dict]


class PolicyApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", description="Digest exacto de la propuesta revisada; incluye perfiles y cuotas.")


class ProfileResponse(BaseModel):
    profile: str
    image: str
    network_mode: str
    bridge: str
    vlan_tag: int | None
    nodes: list[str]
    minimum_disk_gb: int
    max_vcpus_per_instance: int
    max_memory_mb_per_instance: int
    max_disk_gb_per_instance: int
    self_service_enabled: bool
    guest_initialization: str | None = None
    root_password_bootstrap: bool = False
    quotas: dict[str, int]


class UsageResponse(BaseModel):
    tenant_id: str
    usage: dict[str, int]
    description: str = "Incluye reservas fallidas e inciertas y todos los perfiles del tenant; se libera capacidad solo con deleted verificado."


def guarded(call):
    try:
        return call()
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except QueueUnavailable:
        raise HTTPException(503, "LAB_DISPATCH_UNCONFIRMED; inspect operation and resume the SAME instance") from None


def owned(session, identifier, principal):
    row = session.scalar(select(LabPolicy).where(LabPolicy.id == identifier, LabPolicy.tenant_id == principal.tenant_id))
    if row is None:
        raise HTTPException(404, "Lab policy not found")
    return row


@router.get("/v1/profiles", response_model=list[ProfileResponse], operation_id="listAuthorizedProfiles", tags=["instances"],
    summary="Consultar plantillas, destinos y límites autorizados")
def profiles(principal: Reader):
    from ..terraform.profiles import load_profile
    from ..terraform.config import PlanError
    path = os.environ.get("IACP_TERRAFORM_PROFILES_FILE")
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text())
        result = []
        for name, raw in sorted(data["profiles"].items()):
            if principal.tenant_id not in raw.get("allowed_tenants", []):
                continue
            p = load_profile(name, principal.tenant_id)
            result.append(ProfileResponse(profile=name, image=p.settings.image, network_mode=p.settings.network_mode,
                bridge=p.settings.network_bridge, vlan_tag=p.settings.vlan_id, nodes=p.target_nodes or [p.settings.node_name],
                minimum_disk_gb=p.settings.boot_disk.minimum_size_gb, max_vcpus_per_instance=p.max_vcpus,
                max_memory_mb_per_instance=p.max_memory_mb, max_disk_gb_per_instance=p.max_disk_gb,
                self_service_enabled=p.self_service_enabled,guest_initialization=p.guest_initialization,
                root_password_bootstrap=p.root_password_bootstrap, quotas={"instances":p.max_live_instances,
                    "vcpus":p.max_total_vcpus, "memory_mb":p.max_total_memory_mb, "disk_gb":p.max_total_disk_gb,
                    "concurrent_operations":p.max_concurrent_operations}))
        return result
    except (OSError, ValueError, KeyError, TypeError):
        raise PlanError("AUTHORIZED_PROFILE_REQUIRED") from None


@router.get("/v1/lab/usage", tags=["lab-administration"], response_model=UsageResponse, operation_id="getLabUsage")
def usage(principal: Reader, session: Database):
    return UsageResponse(tenant_id=principal.tenant_id, usage=lab.usage(session, principal.tenant_id))


@router.post("/v1/lab/policies", tags=["lab-administration"], response_model=PolicyResponse, status_code=201, operation_id="proposeLabPolicy",
    description="Operator propone límites dentro de los techos del servidor. No autoriza infraestructura. Un approver distinto revisa el digest una vez; la delegación solo cubre VMs nuevas.")
def propose(body: lab.LabPolicySpec, key: Key, principal: Operator, session: Database):
    return guarded(lambda: lab.propose(session, principal, body, key))


@router.get("/v1/lab/policies", tags=["lab-administration"], response_model=list[PolicyResponse], operation_id="listLabPolicies")
def list_policies(principal: Reader, session: Database, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                  offset: Annotated[int, Query(ge=0)] = 0):
    return session.scalars(select(LabPolicy).where(LabPolicy.tenant_id == principal.tenant_id)
        .order_by(LabPolicy.created_at, LabPolicy.id).limit(limit).offset(offset)).all()


@router.get("/v1/lab/policies/{policy_id}", tags=["lab-administration"], response_model=PolicyResponse, operation_id="getLabPolicy")
def get_policy(policy_id: str, principal: Reader, session: Database):
    return owned(session, policy_id, principal)


@router.post("/v1/lab/policies/{policy_id}/approve", tags=["lab-administration"], response_model=PolicyResponse, operation_id="approveLabPolicy",
    description="Approver independiente del proponente. Aprueba el digest exacto y fija la caducidad; no elimina controles de Apply, Lifecycle, DELETE o propiedad.")
def approve(policy_id: str, body: PolicyApproval, principal: Approver, session: Database):
    return guarded(lambda: lab.approve_policy(session, owned(session, policy_id, principal), principal, body.policy_sha256))


@router.post("/v1/lab/policies/{policy_id}/renew", tags=["lab-administration"],response_model=PolicyResponse,operation_id="renewLabPolicy",
    description="Nueva revisión independiente del mismo digest y perfiles. Extiende la ventana por la duración original, conserva aprobación inicial, membresía e historial. Una política revocada no se reactiva.")
def renew(policy_id:str,body:PolicyApproval,principal:Approver,session:Database):
    return guarded(lambda:lab.renew_policy(session,owned(session,policy_id,principal),principal,body.policy_sha256))


@router.post("/v1/lab/policies/{policy_id}/revoke", tags=["lab-administration"], response_model=PolicyResponse, operation_id="revokeLabPolicy",
    description="Revocación irreversible del approver. Bloquea nuevos comandos antes de la mutación; la observación de operaciones ya consumidas sigue disponible.")
def revoke(policy_id: str, principal: Approver, session: Database, body: PowerRequest | None = None):
    with admission_lock(session):
        row = owned(session, policy_id, principal)
        row = session.get(LabPolicy, row.id, with_for_update=True, populate_existing=True)
        if row.status != "revoked":
            row.status, row.revoked_at, row.revoked_by = "revoked", utcnow(), principal.subject
            lab.record_review(row,"revoked",principal.subject)
            session.commit()
        return row


@router.post("/v1/instances/{instance_id}/provision", response_model=PowerAccepted, status_code=202,
    operation_id="resumeLabProvisioning", tags=["instances"], summary="Continuar o reconciliar el aprovisionamiento autorizado",
    description="Mismo UUID y operación de creación. Si el plan ya fue validado, usa la delegación aprobada y el plan guardado. "
    "Si la mutación comenzó, solo observa; jamás crea otra VM. Una planificación fallida se recupera con retry-provision y expected_attempt.")
def provision(instance_id: str, principal: Operator, session: Database, body: PowerRequest | None = None,
              publish=Depends(get_apply_publisher), plan_publish=Depends(get_plan_publisher)):
    from .power import owned as own_instance
    instance = own_instance(session, principal, instance_id)
    def resume():
        execution = session.get(TerraformExecution, instance.id)
        if not instance.self_service_policy_id or execution is None:
            raise Conflict("LAB_POLICY_REQUIRED")
        operation = session.get(Operation, execution.operation_id)
        # Recovery after consumption remains readonly even if policy was revoked.
        from ..models import TerraformApply
        from ..terraform.apply import recover_apply
        approval = session.scalar(select(TerraformApply).where(TerraformApply.instance_id == instance.id,
            TerraformApply.attempt == execution.attempt))
        if approval and approval.status in {"running", "uncertain"}:
            recover_apply(session, execution, approval.id, principal, publish)
        elif approval and approval.status == 'completed' and instance.start_on_create:
            # Release request transaction before the existing admission locks.
            from ..services.fulfillment import advance
            from sqlalchemy.orm import sessionmaker
            sessions = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
            session.commit()
            advance(instance.id, sessions)
        else:
            lab.member(session, instance, "create", current_profiles=True)
            if execution.status == "queued":
                plan_publish(execution.job_id, execution.attempt)
            else:
                lab.advance(session, instance, publish)
        return PowerAccepted(instance_id=instance.id, operation_id=operation.id, job_id=execution.job_id, status=operation.status)
    return guarded(resume)


from .terraform import RetryRequest, PlanQueued


@router.post("/v1/instances/{instance_id}/retry-provision", response_model=PlanQueued, status_code=202,
    operation_id="retryLabProvisioning", tags=["instances"],
    description="Recupera solo un fallo de planificación sin Apply consumido. Conserva UUID, VMID, asignación y state. expected_attempt e Idempotency-Key cercan reintentos concurrentes.")
def retry_provision(instance_id: str, body: RetryRequest, key: Key, principal: Operator, session: Database,
                    publish=Depends(get_plan_publisher)):
    from .power import owned as own_instance
    from ..terraform.actions import retry
    instance = own_instance(session, principal, instance_id)
    def request():
        lab.member(session, instance, "create", current_profiles=True)
        execution = session.get(TerraformExecution, instance.id)
        return retry(session, execution, body.expected_attempt, key, principal.subject, publish)
    return guarded(request)


@router.post("/v1/instances/{instance_id}/reconcile", response_model=PowerAccepted, status_code=202,
    operation_id="reconcileInstance", tags=["operations"], summary="Reconciliar una mutación incierta solo por observación",
    description="Consulta state/Proxmox o monitorea el UPID ya consumido. Nunca repite aprovisionamiento, energía ni DELETE. "
    "Disponible con admisión o política cerradas. Conserva UUID, VMID y auditoría; 409 si no existe mutación recuperable.")
def reconcile(instance_id: str, principal: Operator, session: Database, body: PowerRequest | None = None,
    publish=Depends(get_apply_publisher),guest_publish=Depends(get_guest_initialization_publisher)):
    from .power import owned as own_instance
    from .dependencies import get_deletion_publisher, get_power_publisher
    from ..models import DeletionExecution, PowerExecution, TerraformApply
    from ..terraform.apply import recover_apply
    from ..terraform.deletion import recover_or_cancel
    from ..lifecycle.service import response
    instance=own_instance(session,principal,instance_id)
    def request():
        from ..models import GuestInitialization
        initialization=session.scalar(select(GuestInitialization).where(GuestInitialization.instance_id==instance.id,
            GuestInitialization.status.in_(['running','uncertain']),GuestInitialization.started_at.is_not(None)))
        if initialization:
            guest_publish(initialization.operation_id)
            return response(session,session.get(Operation,initialization.operation_id))
        deletion=session.scalar(select(DeletionExecution).where(DeletionExecution.instance_id==instance.id,
            DeletionExecution.status.in_(['running','uncertain']),DeletionExecution.started_at.is_not(None)))
        if deletion:
            return recover_or_cancel(session,instance,principal,deletion.operation_id,get_deletion_publisher())
        approval=session.scalar(select(TerraformApply).where(TerraformApply.instance_id==instance.id,
            TerraformApply.status.in_(['queued','running','uncertain'])))
        if approval:
            execution=session.get(TerraformExecution,instance.id)
            recover_apply(session,execution,approval.id,principal,publish)
            return response(session,session.get(Operation,execution.operation_id))
        power=session.scalar(select(PowerExecution).where(PowerExecution.instance_id==instance.id,
            PowerExecution.status.in_(['running','uncertain']),PowerExecution.submitted_at.is_not(None)))
        if power:
            get_power_publisher()(power.operation_id)
            return response(session,session.get(Operation,power.operation_id))
        raise Conflict('RECONCILIATION_NOT_REQUIRED')
    return guarded(request)
