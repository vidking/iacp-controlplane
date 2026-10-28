from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Instance, Job, Operation, OperationEvent
from ..schemas import InstanceAccepted


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
