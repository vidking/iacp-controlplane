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
    # Preserve hashes for pre-0011 requests, including explicitly supplied defaults.
    optional={"network_profile", "node_preference", "self_service_policy_id", "commercial_context"}
    values=spec.model_dump(exclude={'access'} | {name for name in optional if getattr(spec,name) is None})
    if not spec.start_on_create:
        values.pop('start_on_create')
    canonical = dict(values)
    if spec.access:
        from .access import password_fingerprint
        canonical['access'] = {'username': 'root', 'fingerprint': password_fingerprint(spec.access.password.get_secret_value())}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    query = select(Operation).where(Operation.tenant_id == principal.tenant_id, Operation.idempotency_key == key)
    previous = session.scalar(query)
    if previous is not None:
        return accepted(session, previous, digest)
    from ..terraform.config import instance_executor
    mode = instance_executor()
    if spec.start_on_create:
        if mode != 'terraform' or not spec.self_service_policy_id:
            raise Conflict('AUTOSTART_REQUIRES_SELF_SERVICE_POLICY')
        from ..lifecycle.ownership import mutation_revision
        from ..lifecycle.proxmox import PowerError
        try:
            mutation_revision()
        except PowerError as failure:
            raise Conflict(failure.code) from None
    if spec.node_preference and not spec.network_profile:
        raise Conflict("NODE_PREFERENCE_REQUIRES_PROFILE")
    if spec.self_service_policy_id and (not spec.network_profile or mode != "terraform"):
        raise Conflict("LAB_PROFILE_REQUIRED")
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
    from ..terraform.locks import ExecutionBusy, admission_lock
    from ..terraform.profiles import reserve_profile
    # Reuse the existing advisory lock utility; commit the VMID with the intent.
    guard=admission_lock(session) if spec.network_profile and mode=='terraform' else nullcontext()
    try:
        with guard:
            previous=session.scalar(query)
            if previous is not None:
                return accepted(session,previous,digest)
            if spec.network_profile and not spec.self_service_policy_id:
                from ..terraform.profiles import load_profile
                if load_profile(spec.network_profile,principal.tenant_id).self_service_enabled:
                    raise Conflict('LAB_POLICY_REQUIRED')
            from .lab import admit, instance_digest
            policy=admit(session,instance) if spec.self_service_policy_id else None
            if spec.network_profile:
                from ..terraform.profiles import load_profile
                if load_profile(spec.network_profile, principal.tenant_id).root_password_bootstrap and not spec.access:
                    raise Conflict('ROOT_ACCESS_REQUIRED_BY_PROFILE')
            if spec.access:
                from .access import admit_access, encrypt_password
                admit_access(spec, principal.tenant_id)
                instance.initial_access_ciphertext = encrypt_password(instance, spec.access.password.get_secret_value())
                instance.initial_access_fingerprint = canonical['access']['fingerprint']
            if spec.start_on_create and 'start' not in policy.specification['actions']:
                raise Conflict('AUTOSTART_NOT_AUTHORIZED')
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
                if policy:
                    execution=session.get(TerraformExecution,instance.id)
                    instance.policy_binding_sha256=instance_digest(instance,execution,policy)
                    event(session,operation,"lab_policy_bound", "policy:"+policy.id)
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
        if spec.access:
            raise Conflict('INITIAL_PASSWORD_ALREADY_USED') from None
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
