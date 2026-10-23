"""Deletion admission, independent approval and one-shot mutation/recovery fence."""
from datetime import timedelta
import os
from types import SimpleNamespace
import time
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..lifecycle.certification_binding import binding_digest
from ..lifecycle.service import managed_target
from ..models import DeletionExecution, Instance, Job, Operation, PowerExecution, TerraformApply, TerraformExecution, Tenant, utcnow
from ..services.instances import Conflict, event
from ..services.jobs import QueueUnavailable
from .apply import fingerprint, not_expired
from .config import Allocation, PlanError, SOURCE_REVISION
from .deletion_executor import DeletionExecutor
from .lifecycle import build_executor
from .locks import ExecutionBusy, instance_lock


def gate(instance):
    if os.environ.get('IACP_TERRAFORM_DELETE_ENABLED') != '1' or os.environ.get('IACP_TERRAFORM_APPLY_ENABLED') != '1':
        raise Conflict('DELETE_DISABLED')
    try:
        allowed = {str(UUID(x.strip())) for x in os.environ.get('IACP_DISPOSABLE_INSTANCE_IDS', '').split(',') if x.strip()}
    except ValueError:
        raise Conflict('DELETE_ALLOWLIST_INVALID') from None
    if instance.id not in allowed or instance.vmid is None or instance.vmid < 30000 or instance.vmid in {104, 106}:
        raise Conflict('DELETE_DISPOSABLE_INSTANCE_REQUIRED')


def active_deletion(session, instance_id):
    return session.scalar(select(DeletionExecution.operation_id).where(
        DeletionExecution.instance_id == instance_id, DeletionExecution.status != 'failed'))


def context(session, row):
    instance = session.get(Instance, row.instance_id, populate_existing=True)
    operation = session.get(Operation, row.operation_id)
    job = session.scalar(select(Job).where(Job.operation_id == operation.id))
    if (instance is None or job is None or instance.tenant_id != operation.tenant_id
            or job.tenant_id != operation.tenant_id or operation.instance_id != instance.id
            or operation.kind != 'instance.delete' or job.operation != 'instance.delete'
            or row.vmid < 30000 or row.vmid in {104, 106}
            or instance.status != ('deleting' if row.started_at else 'ready')):
        raise PlanError('DELETE_BINDING_CHANGED')
    # Deleting is an explicit tombstone transition, not a rewrite of provisioning.
    original = SimpleNamespace(**{k:v for k,v in vars(instance).items() if not k.startswith('_')})
    original.status = 'ready'
    try:
        target, approval = managed_target(session, original)
    except Conflict:
        raise PlanError('DELETE_BINDING_CHANGED') from None
    execution = session.get(TerraformExecution, instance.id)
    if ((target.vmid, target.node, target.name) != (row.vmid, row.node, row.name)
            or execution.source_revision != SOURCE_REVISION or not approval.started_at or not approval.finished_at
            or binding_digest(original, execution, approval) != row.provisioning_binding
            or execution.state_key != f'iacp/lab/tenants/{instance.tenant_id}/instances/{instance.id}/terraform.tfstate'):
        raise PlanError('DELETE_BINDING_CHANGED')
    allocation = Allocation.model_validate(execution.allocation)
    variables = allocation.variables(instance)
    if (allocation.vm_id, allocation.node_name) != (row.vmid, row.node):
        raise PlanError('DELETE_BINDING_CHANGED')
    return {'instance_id': instance.id, 'operation_id': row.operation_id, 'attempt': execution.attempt,
            'provisioning_plan_sha256': execution.plan_sha256, 'state_key': execution.state_key,
            'allocation': execution.allocation, 'variables': variables, 'backend': allocation.backend.arguments(execution.state_key)}


def accepted(session, operation):
    job = session.scalar(select(Job).where(Job.operation_id == operation.id))
    return {'instance_id': operation.instance_id, 'operation_id': operation.id, 'job_id': job.id, 'status': operation.status}


def publish_task(session, operation, publish, mode):
    try:
        publish(operation.id, mode)
    except Exception:
        event(session, operation, 'delete_dispatch_unconfirmed', 'api:terraform')
        session.commit()
        raise QueueUnavailable from None


def plan_request(session, instance, principal, key, publish):
    query = select(Operation).where(Operation.tenant_id == principal.tenant_id, Operation.idempotency_key == key)
    digest = fingerprint({'instance_id': instance.id, 'action': 'delete'})
    try:
        with instance_lock(session.get_bind(), instance.id) as heartbeat:
            instance = session.get(Instance, instance.id, with_for_update=True, populate_existing=True)
            previous = session.scalar(query)
            if previous:
                if previous.kind != 'instance.delete' or previous.requested_by != principal.subject or previous.request_hash != digest:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                result = accepted(session, previous)
                resume = session.get(DeletionExecution, previous.id).phase == 'plan_queued'
                identifier = previous.id
            else:
                gate(instance)
                target, approval = managed_target(session, instance)
                if active_deletion(session, instance.id):
                    raise Conflict('DELETE_OPERATION_ACTIVE')
                if session.scalar(select(PowerExecution.operation_id).where(
                        PowerExecution.instance_id == instance.id, PowerExecution.status.in_(['queued','running','uncertain']))):
                    raise Conflict('POWER_OPERATION_ACTIVE')
                if session.scalar(select(TerraformApply.id).where(TerraformApply.instance_id == instance.id,
                        TerraformApply.status.in_(['approved','queued','running','uncertain']))):
                    raise Conflict('APPLY_REQUIRES_RECONCILIATION')
                execution = session.get(TerraformExecution, instance.id)
                operation = Operation(id=str(uuid4()), instance_id=instance.id, tenant_id=principal.tenant_id,
                    kind='instance.delete', status='queued', idempotency_key=key, request_hash=digest, requested_by=principal.subject)
                session.add(operation);session.flush()
                session.add(Job(id=str(uuid4()), operation=operation.kind, tenant_id=principal.tenant_id,
                                operation_id=operation.id, status='queued'))
                row = DeletionExecution(operation_id=operation.id, instance_id=instance.id, status='queued', phase='plan_queued',
                    vmid=target.vmid, node=target.node, name=target.name,
                    provisioning_binding=binding_digest(instance, execution, approval))
                session.add(row);session.flush();context(session, row)
                event(session, operation, 'delete_plan_accepted', principal.subject);heartbeat();session.commit()
                identifier, resume = operation.id, True
                result = accepted(session, operation)
    except ExecutionBusy:
        raise Conflict('INSTANCE_BUSY') from None
    except IntegrityError:
        session.rollback()
        raise Conflict('DELETE_OPERATION_ACTIVE_OR_KEY_CONFLICT') from None
    if resume:
        publish_task(session, session.get(Operation, identifier), publish, 'plan')
    return result


def owned_plan(session, instance_id, operation_id):
    row = session.get(DeletionExecution, operation_id, with_for_update=True, populate_existing=True)
    if row is None or row.instance_id != instance_id:
        raise Conflict('DELETE_PLAN_NOT_FOUND')
    return row


def approve(session, instance, principal, body, key):
    try:
        with instance_lock(session.get_bind(), instance.id):
            row = owned_plan(session, instance.id, body['operation_id'])
            operation = session.get(Operation, row.operation_id)
            digest = fingerprint(body)
            if row.approval_key:
                if row.approval_key != key or row.approval_hash != digest or row.approved_by != principal.subject:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                return accepted(session, operation)
            gate(instance);context(session, row)
            if principal.subject == operation.requested_by:
                raise Conflict('INDEPENDENT_APPROVAL_REQUIRED')
            if row.status != 'planned' or row.plan_sha256 != body['plan_sha256'] or row.started_at:
                raise Conflict('DELETE_PLAN_CHANGED')
            row.approval_key, row.approval_hash, row.approved_by = key, digest, principal.subject
            row.expires_at = utcnow() + timedelta(seconds=body['valid_for_seconds'])
            row.status, row.phase = 'approved', 'approved'
            event(session, operation, 'delete_plan_approved', principal.subject);session.commit()
            return accepted(session, operation)
    except ExecutionBusy:
        raise Conflict('INSTANCE_BUSY') from None


def dispatch(session, instance, principal, body, key, publish):
    try:
        with instance_lock(session.get_bind(), instance.id):
            row = owned_plan(session, instance.id, body['operation_id'])
            operation = session.get(Operation, row.operation_id)
            if (operation.requested_by != principal.subject or operation.idempotency_key != key
                    or row.plan_sha256 != body['plan_sha256']):
                raise Conflict('IDEMPOTENCY_CONFLICT')
            if row.status == 'completed':
                return accepted(session, operation)
            context(session, row)
            if row.started_at:
                mode = 'reconcile'
            else:
                gate(instance)
                if (row.status not in {'approved','queued'} or row.phase not in {'approved','delete_queued'}
                        or not row.approved_by or row.approved_by == principal.subject
                        or not row.expires_at or not not_expired(row.expires_at)):
                    raise Conflict('DELETE_APPROVAL_REQUIRED_OR_EXPIRED')
                mode = 'apply'
                row.status, row.phase, operation.status = 'queued', 'delete_queued', 'queued'
                session.scalar(select(Job).where(Job.operation_id == operation.id)).status = 'queued'
                event(session, operation, 'delete_queued', principal.subject);session.commit()
            result = accepted(session, operation)
    except ExecutionBusy:
        raise Conflict('INSTANCE_BUSY') from None
    publish_task(session, operation, publish, mode)
    return result


def recover_or_cancel(session, instance, principal, identifier, publish, *, cancel=False):
    try:
        with instance_lock(session.get_bind(), instance.id):
            row = owned_plan(session, instance.id, identifier)
            operation = session.get(Operation, identifier)
            if operation.requested_by != principal.subject:
                raise Conflict('DELETE_ORIGINAL_OPERATOR_REQUIRED')
            if cancel:
                if row.started_at:
                    raise Conflict('DELETE_RECONCILIATION_REQUIRED')
                if row.status == 'failed':
                    return accepted(session, operation)
                context(session, row)
                row.status = operation.status = 'failed';row.phase = 'cancelled';row.finished_at = utcnow()
                operation.error_code = 'DELETE_CANCELLED'
                session.scalar(select(Job).where(Job.operation_id == identifier)).status = 'failed'
                event(session, operation, 'delete_cancelled', principal.subject);session.commit()
                return accepted(session, operation)
            if row.status == 'completed':
                return accepted(session, operation)
            if not row.started_at or row.status not in {'running','uncertain'}:
                raise Conflict('DELETE_RECOVERY_NOT_REQUIRED')
            context(session, row)
            result = accepted(session, operation)
    except ExecutionBusy:
        raise Conflict('INSTANCE_BUSY') from None
    publish_task(session, operation, publish, 'reconcile')
    return result


def execute_deletion(identifier, mode, sessions, executor=None):
    if mode not in {'plan','apply','reconcile'}:
        return
    with sessions() as session:
        row = session.get(DeletionExecution, identifier)
        if row is None:
            return
        instance_id = row.instance_id
    try:
        with instance_lock(sessions.kw['bind'], instance_id) as heartbeat:
            _execute(identifier, mode, sessions, executor, heartbeat)
    except ExecutionBusy:
        return


def _execute(identifier, mode, sessions, executor, heartbeat):
    with sessions() as session:
        row = session.get(DeletionExecution, identifier, with_for_update=True)
        if row.status in {'failed','completed'}:
            return
        if mode == 'plan' and row.status == 'running' and row.phase == 'planning':
            # We hold the shared lock: a prior planner is no longer alive. Keep
            # its partial artifacts, fail this operation, never overwrite its plan.
            row.status = row.phase = 'failed'
            operation = session.get(Operation, identifier)
            operation.status, operation.error_code = 'failed', 'DELETE_PLAN_INTERRUPTED'
            session.scalar(select(Job).where(Job.operation_id == identifier)).status = 'failed'
            event(session, operation, 'delete_plan_interrupted', 'worker:terraform');session.commit()
            return
        if mode == 'plan' and (row.status != 'queued' or row.phase != 'plan_queued'):
            return
        if mode != 'plan' and (row.phase not in {'delete_queued','applying','uncertain'}):
            return
        if mode == 'reconcile' and not row.started_at:
            return
        # Redelivery after dispatch only observes; it never consumes a second command.
        mode = 'reconcile' if row.started_at else mode
        row.status = 'running';row.phase = 'planning' if mode == 'plan' else 'applying'
        operation = session.get(Operation, identifier);operation.status = 'running'
        job = session.scalar(select(Job).where(Job.operation_id == identifier));job.status = 'running'
        event(session, operation, 'delete_' + mode + '_started', 'worker:terraform');session.commit()
    deadline = time.monotonic() + 900
    def check():
        heartbeat()
        if time.monotonic() >= deadline:
            raise PlanError('EXECUTION_DEADLINE_EXCEEDED')
        with sessions() as session:
            row = session.get(DeletionExecution, identifier)
            tenant = session.get(Tenant, session.get(Instance, row.instance_id).tenant_id)
            if not tenant or not tenant.active:
                raise PlanError('TENANT_INACTIVE')
            context(session, row)
            if mode != 'reconcile':
                gate(session.get(Instance, row.instance_id))
        return False
    def before_mutation():
        check()
        with sessions() as session:
            row = session.get(DeletionExecution, identifier, with_for_update=True)
            if row.started_at or not row.expires_at or not not_expired(row.expires_at):
                raise PlanError('DELETE_APPROVAL_EXPIRED_OR_CONSUMED')
            if not row.approved_by or row.approved_by == session.get(Operation, identifier).requested_by:
                raise PlanError('INDEPENDENT_APPROVAL_REQUIRED')
            row.started_at = utcnow()
            session.get(Instance, row.instance_id).status = 'deleting'
            event(session, session.get(Operation, identifier), 'delete_mutation_started', 'worker:terraform');session.commit()
    result, error = None, None
    try:
        check()
        with sessions() as session:
            row = session.get(DeletionExecution, identifier);ctx = context(session, row);sha = row.plan_sha256
        if executor is None:
            base = build_executor(observation=mode != 'plan')
            executor = DeletionExecutor(base.source, base.artifacts, base.runner)
        if mode == 'plan':result = executor.plan_deletion(ctx, check)
        elif mode == 'apply':result = executor.apply_deletion(ctx, sha, check, before_mutation)
        else:result = executor.reconcile_deletion(ctx, sha, check)
        check()
        if mode != 'plan' and not (result.get('deleted') is True and result.get('state_present') is False and result.get('proxmox_present') is False):
            raise PlanError('DELETE_ABSENCE_UNCONFIRMED')
    except Exception as failure:
        error = failure.code if isinstance(failure, PlanError) else str(failure) if isinstance(failure, Conflict) else 'DELETE_EXECUTION_FAILED'
    with sessions() as session:
        row = session.get(DeletionExecution, identifier, with_for_update=True)
        operation = session.get(Operation, identifier);job = session.scalar(select(Job).where(Job.operation_id == identifier))
        if error:
            row.status = operation.status = 'uncertain' if row.started_at else 'failed'
            row.phase = row.status;operation.error_code = error;job.status = 'failed'
            event(session, operation, 'delete_' + row.status, 'worker:terraform')
        elif mode == 'plan':
            row.status, row.phase, row.plan_sha256, row.result = 'planned','awaiting_approval',result['plan_sha256'],result
            job.status = 'completed';event(session, operation, 'delete_plan_ready', 'worker:terraform')
        else:
            row.status = operation.status = job.status = 'completed';row.phase = 'deleted'
            row.result, row.finished_at, operation.error_code = result, utcnow(), None
            session.get(Instance, row.instance_id).status = 'deleted'
            event(session, operation, 'delete_completed', 'worker:terraform')
        session.commit()
