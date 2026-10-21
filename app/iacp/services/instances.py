import hashlib
import json
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Instance, Job, Operation, OperationEvent
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
    return InstanceAccepted(instance_id=operation.instance_id, operation_id=operation.id, job_id=job.id)


def create_instance(session: Session, principal: Principal, spec: InstanceCreate,
                    key: str, publish: Publisher) -> InstanceAccepted:
    digest = hashlib.sha256(json.dumps(spec.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    query = select(Operation).where(Operation.tenant_id == principal.tenant_id, Operation.idempotency_key == key)
    previous = session.scalar(query)
    if previous is not None:
        return accepted(session, previous, digest)
    instance = Instance(id=str(uuid4()), tenant_id=principal.tenant_id, status="pending", **spec.model_dump())
    operation = Operation(
        id=str(uuid4()), tenant_id=principal.tenant_id, instance_id=instance.id, kind="instance.create",
        status="queued", idempotency_key=key, request_hash=digest, requested_by=principal.subject,
    )
    job = Job(id=str(uuid4()), operation="instance.create", status="queued",
              tenant_id=principal.tenant_id, operation_id=operation.id)
    try:
        session.add(instance)
        session.flush()
        session.add(operation)
        session.flush()
        session.add(job)
        event(session, operation, "accepted", principal.subject)
        session.commit()
    except IntegrityError:
        session.rollback()
        # La restricción UNIQUE arbitra incluso solicitudes concurrentes entre APIs.
        previous = session.scalar(query)
        if previous is not None:
            return accepted(session, previous, digest)
        if session.scalar(select(Instance.id).where(Instance.tenant_id == principal.tenant_id, Instance.name == spec.name)):
            raise Conflict("Instance name already exists") from None
        raise
    result = InstanceAccepted(instance_id=instance.id, operation_id=operation.id, job_id=job.id)
    try:
        publish(job.id)
    except Exception:
        # Un timeout puede ser ambiguo. No sobrescribir un worker que ya reclamó el job.
        changed = session.execute(update(Job).where(Job.id == job.id, Job.status == "queued").values(
            status="failed", message="No se confirmó la publicación del trabajo",
        )).rowcount
        if changed:
            operation.status = "failed"
            operation.error_code = "DISPATCH_FAILED"
            instance.status = "failed"
            event(session, operation, "dispatch_failed", principal.subject)
        session.commit()
        raise QueueUnavailable from None
    return result
