import hashlib
import json
from uuid import uuid4
from contextlib import nullcontext

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Instance, Job, Operation, OperationEvent, TerraformExecution
from ..schemas import InstanceAccepted, InstanceCreate
from ..security import Principal
from .jobs import Publisher, QueueUnavailable


class Conflict(Exception):
    pass


def event(session: Session, operation: Operation, code: str, actor: str) -> None:
    session.add(OperationEvent(operation_id=operation.id, status=operation.status, code=code, actor=actor))


def accepted(session: Session, operation: Operation, digest: str) -> InstanceAccepted:
    if operation.request_hash != digest:
        raise Conflict("Idempotency key already used for another request")
    job = session.scalars(select(Job).where(Job.operation_id == operation.id)).one()
    instance = session.get(Instance, operation.instance_id)
    return InstanceAccepted(instance_id=operation.instance_id, operation_id=operation.id, job_id=job.id, executor=instance.executor)


def create_instance(session: Session, principal: Principal, spec: InstanceCreate,
                    key: str, publish: Publisher) -> InstanceAccepted:
    values=spec.model_dump(exclude={"network_profile"} if spec.network_profile is None else set())
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    query = select(Operation).where(Operation.tenant_id == principal.tenant_id, Operation.idempotency_key == key)
    previous = session.scalar(query)
    if previous is not None:
        return accepted(session, previous, digest)
    from ..terraform.config import instance_executor
    mode = instance_executor()
    if spec.network_profile and mode != "terraform":
        from ..terraform.config import PlanError
        raise PlanError("PROFILE_REQUIRES_TERRAFORM")
    instance = Instance(id=str(uuid4()), tenant_id=principal.tenant_id, status="pending", executor=mode, **values)
    operation = Operation(
        id=str(uuid4()), tenant_id=principal.tenant_id, instance_id=instance.id, kind="instance.create",
        status="queued", idempotency_key=key, request_hash=digest, requested_by=principal.subject,
    )
    job = Job(id=str(uuid4()), operation="instance.plan" if mode == "terraform" else "instance.create", status="queued",
              tenant_id=principal.tenant_id, operation_id=operation.id)
    from ..terraform.locks import ExecutionBusy, instance_lock
    from ..terraform.profiles import reserve_profile
    # Reuse the existing advisory lock utility; commit the VMID with the intent.
    guard=instance_lock(session.get_bind(), 'machine-profile:vmid-reservations') if spec.network_profile and mode=='terraform' else nullcontext()
    try:
        with guard:
            previous=session.scalar(query)
            if previous is not None:
                return accepted(session,previous,digest)
            allocation=reserve_profile(session,instance) if spec.network_profile and mode=='terraform' else None
            session.add(instance)
            session.flush()
            session.add(operation)
            session.flush()
            session.add(job)
            if mode == "terraform":
                from ..terraform.lifecycle import initial_execution
                session.flush()
                initial_execution(session, instance, operation, job, allocation)
            event(session, operation, "accepted", principal.subject)
            session.commit()
    except ExecutionBusy:
        raise Conflict("PROFILE_BUSY") from None
    except IntegrityError:
        session.rollback()
        # La restricción UNIQUE arbitra incluso solicitudes concurrentes entre APIs.
        previous = session.scalar(query)
        if previous is not None:
            return accepted(session, previous, digest)
        if session.scalar(select(Instance.id).where(Instance.tenant_id == principal.tenant_id, Instance.name == spec.name)):
            raise Conflict("Instance name already exists") from None
        raise
    result = InstanceAccepted(instance_id=instance.id, operation_id=operation.id, job_id=job.id, executor=mode)
    try:
        publish(job.id)
    except Exception:
        # Un timeout puede ser ambiguo. No sobrescribir un worker que ya reclamó el job.
        execution = None
        if mode == "terraform":
            # Igual que el worker: bloquear ejecución antes de job para evitar deadlocks.
            execution = session.get(TerraformExecution, instance.id, with_for_update=True, populate_existing=True)
        changed = session.execute(update(Job).where(Job.id == job.id, Job.status == "queued").values(
            status="failed", message="No se confirmó la publicación del trabajo",
        )).rowcount
        if changed:
            operation.status = "failed"
            operation.error_code = "DISPATCH_FAILED"
            instance.status = "failed"
            if mode == "terraform":
                from ..terraform.lifecycle import fail_dispatch
                fail_dispatch(session, execution)
            event(session, operation, "dispatch_failed", principal.subject)
        session.commit()
        raise QueueUnavailable from None
    return result
