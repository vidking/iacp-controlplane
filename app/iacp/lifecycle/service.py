"""Admission and durable lifecycle state; uses the existing per-instance lock."""
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models import Instance, Job, Operation, PowerExecution, TerraformApply, TerraformExecution
from ..schemas import PowerAccepted
from ..services.instances import Conflict, event
from ..services.jobs import QueueUnavailable
from ..terraform.locks import ExecutionBusy, instance_lock, admission_lock
from .ownership import instance_ownership, mutation_revision
from .proxmox import PowerError, Target

ACTIVE = {"queued", "running", "uncertain"}


def managed_target(session, instance):
    if instance.status != "ready" or instance.executor != "terraform":
        raise Conflict("POWER_MANAGED_KVM_REQUIRED")
    target = Target(instance.vmid, instance.node or "", instance.name)
    execution = session.get(TerraformExecution, instance.id)
    approval = None if execution is None else session.scalar(select(TerraformApply).where(
        TerraformApply.instance_id == instance.id, TerraformApply.attempt == execution.attempt,
        TerraformApply.plan_sha256 == execution.plan_sha256, TerraformApply.status == "completed"))
    result = (approval.result or {}) if approval else {}
    observation = result.get("observation", {})
    if (approval is None or execution.vmid != target.vmid
            or result.get("simulated") is not False or result.get("vmid") != target.vmid
            or result.get("node") != target.node or result.get("name") != target.name
            or observation.get("postconditions_verified") is not True
            or observation.get("state_present") is not True or observation.get("proxmox_present") is not True):
        raise Conflict("POWER_PROVISIONING_EVIDENCE_REQUIRED")
    return target, approval


def binding(session, row, operation, *, check_ownership=False):
    instance = session.get(Instance, row.instance_id, populate_existing=True)
    job = session.scalar(select(Job).where(Job.operation_id == operation.id))
    if (instance is None or job is None or row.action not in {"start", "shutdown", "reboot"}
            or instance.tenant_id != operation.tenant_id or job.tenant_id != operation.tenant_id
            or operation.instance_id != instance.id or operation.kind != "instance.power." + row.action
            or job.operation != operation.kind):
        raise PowerError("POWER_BINDING_CHANGED")
    try:
        target, approval = managed_target(session, instance)
    except (Conflict, PowerError):
        raise PowerError("POWER_BINDING_CHANGED") from None
    if (target != Target(row.vmid, row.node, row.name) or approval.id != row.approval_id):
        raise PowerError("POWER_BINDING_CHANGED")
    if check_ownership and instance_ownership(session, instance, approval) != row.ownership_certificate_id:
        raise PowerError("POWER_BINDING_CHANGED")
    if check_ownership and instance.self_service_policy_id:
        from ..services.lab import member
        try:
            member(session, instance, row.action)
        except Conflict as failure:
            raise PowerError(str(failure)) from None
    return target, job


def response(session, operation):
    job = session.scalar(select(Job).where(Job.operation_id == operation.id))
    return PowerAccepted(instance_id=operation.instance_id, operation_id=operation.id,
                         job_id=job.id, status=operation.status)


def request_power(session, instance, principal, action, key, publish, client_factory):
    digest = hashlib.sha256(json.dumps({"instance_id": instance.id, "action": action}, sort_keys=True).encode()).hexdigest()
    query = select(Operation).where(Operation.tenant_id == principal.tenant_id, Operation.idempotency_key == key)

    def previous_result(previous):
        if (previous.request_hash != digest or previous.requested_by != principal.subject
                or previous.kind != "instance.power." + action or previous.instance_id != instance.id):
            raise Conflict("IDEMPOTENCY_CONFLICT")
        result = response(session, previous)
        # A repeated request resumes observation after dispatch, never a second
        # POST. Queued messages may be republished after ambiguous broker errors.
        if previous.status in ACTIVE:
            dispatch(session, previous.id, publish)
        return result

    previous = session.scalar(query)
    if previous:
        return previous_result(previous)
    try:
        with admission_lock(session), instance_lock(session.get_bind(), instance.id) as heartbeat:
            instance = session.get(Instance, instance.id, with_for_update=True, populate_existing=True)
            previous = session.scalar(query)
            if previous:
                # Release the lock before publishing below.
                identifier = previous.id
                session.commit()
            else:
                from ..terraform.deletion import active_deletion
                if active_deletion(session, instance.id):
                    raise Conflict("DELETE_OPERATION_ACTIVE")
                from ..services.guest_identity import active as active_initialization
                if active_initialization(session,instance.id):raise Conflict('GUEST_INITIALIZATION_ACTIVE')
                from ..services.lab import authorize_operation
                authorize_operation(session, instance, action)
                try:
                    revision = mutation_revision()
                except PowerError as failure:
                    raise Conflict(failure.code) from None
                target, approval = managed_target(session, instance)
                try:
                    certificate_id = instance_ownership(session, instance, approval)
                except PowerError as failure:
                    raise Conflict(failure.code) from None
                if session.scalar(select(PowerExecution.operation_id).where(
                        PowerExecution.instance_id == instance.id, PowerExecution.status.in_(ACTIVE))):
                    raise Conflict("POWER_OPERATION_ACTIVE")
                client_factory().permissions(target, mutate=True)
                operation = Operation(id=str(uuid4()), instance_id=instance.id, tenant_id=principal.tenant_id,
                                      kind="instance.power." + action, status="queued", idempotency_key=key,
                                      request_hash=digest, requested_by=principal.subject)
                session.add(operation)
                session.flush()
                session.add(Job(id=str(uuid4()), operation=operation.kind, status="queued",
                                tenant_id=principal.tenant_id, operation_id=operation.id))
                session.add(PowerExecution(operation_id=operation.id, instance_id=instance.id, action=action,
                    status="queued", phase="queued", vmid=target.vmid, node=target.node, name=target.name,
                    approval_id=approval.id, ownership_revision=revision, ownership_certificate_id=certificate_id))
                event(session, operation, "power_accepted", principal.subject)
                heartbeat()
                session.commit()
                identifier = operation.id
    except ExecutionBusy:
        raise Conflict("INSTANCE_BUSY") from None
    except IntegrityError:
        session.rollback()
        previous = session.scalar(query)
        if previous:
            return previous_result(previous)
        raise Conflict("POWER_OPERATION_ACTIVE") from None
    operation = session.get(Operation, identifier, populate_existing=True)
    if operation.request_hash != digest or operation.requested_by != principal.subject:
        raise Conflict("IDEMPOTENCY_CONFLICT")
    result = response(session, operation)
    if operation.status in ACTIVE:
        dispatch(session, identifier, publish)
    return result


def dispatch(session, identifier, publish):
    try:
        publish(identifier)
    except Exception:
        # Publication and Redis acknowledgement are not atomic with PostgreSQL.
        # Keep the durable reservation; retrying the same key safely republishes.
        session.rollback()
        operation = session.get(Operation, identifier, with_for_update=True, populate_existing=True)
        if operation.status == "queued":
            operation.error_code = "POWER_DISPATCH_UNCONFIRMED"
            event(session, operation, "power_dispatch_unconfirmed", "api:lifecycle")
        session.commit()
        raise QueueUnavailable from None
