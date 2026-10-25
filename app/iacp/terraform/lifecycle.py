import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models import Instance, Job, Operation, TerraformAttempt, TerraformExecution, Tenant, utcnow
from ..services.instances import event, Conflict
from .config import Allocation, PlanError, SOURCE_REVISION, allocation_for
from .executor import TerraformExecutor
from .locks import ExecutionBusy, instance_lock


def initial_execution(session, instance, operation, job, allocation=None):
    execution = TerraformExecution(instance_id=instance.id, job_id=job.id, operation_id=operation.id,
                                   status="queued", attempt=1,
                                   state_key=f"iacp/lab/tenants/{instance.tenant_id}/instances/{instance.id}/terraform.tfstate",
                                   allocation=allocation.model_dump() if allocation else None,
                                   vmid=allocation.vm_id if allocation else None,
                                   source_revision=SOURCE_REVISION if allocation else None)
    session.add(execution)
    session.flush()
    session.add(TerraformAttempt(instance_id=instance.id, number=1, status="queued", actor=operation.requested_by))


def fail_dispatch(session, execution, code="DISPATCH_FAILED"):
    execution.status = "failed"
    execution.error_code = code
    attempt = session.scalar(select(TerraformAttempt).where(
        TerraformAttempt.instance_id == execution.instance_id, TerraformAttempt.number == execution.attempt))
    attempt.status, attempt.error_code, attempt.finished_at = "failed", code, utcnow()


def build_executor(*, observation=False):
    if not observation and os.environ.get("IACP_TERRAFORM_PLAN_ENABLED") != "1":
        raise PlanError("REAL_PLAN_DISABLED")
    try:
        return TerraformExecutor(Path(os.environ["IACP_TERRAFORM_SOURCE_DIR"]), Path(os.environ["IACP_TERRAFORM_ARTIFACTS_DIR"]))
    except KeyError:
        raise PlanError("EXECUTOR_CONFIGURATION_REQUIRED") from None


def execute_plan(job_id, number, sessions, executor=None, load_allocation=allocation_for):
    with sessions() as session:
        instance_id = session.scalar(select(TerraformExecution.instance_id).where(TerraformExecution.job_id == job_id))
    if instance_id is None:
        return
    try:
        with instance_lock(sessions.kw["bind"], instance_id):
            _execute_plan(instance_id, job_id, number, sessions, executor, load_allocation)
    except ExecutionBusy:
        # La entrega duplicada no reclama el job del worker que ya lo tiene.
        return


def _execute_plan(instance_id, job_id, number, sessions, executor, load_allocation):
    with sessions() as session:
        execution = session.get(TerraformExecution, instance_id, with_for_update=True)
        if execution.attempt != number or execution.status != "queued":
            return
        job = session.get(Job, job_id)
        operation = session.get(Operation, execution.operation_id)
        instance = session.get(Instance, instance_id)
        if job.status != "queued" or job.operation != "instance.plan" or not (
            job.tenant_id == operation.tenant_id == instance.tenant_id and job.operation_id == operation.id
            and operation.instance_id == instance.id and instance.executor == "terraform"
        ):
            return
        execution.status = job.status = operation.status = "running"
        attempt = session.scalar(select(TerraformAttempt).where(TerraformAttempt.instance_id == instance_id, TerraformAttempt.number == number))
        attempt.status = "running"
        event(session, operation, "terraform_plan_started", "worker:terraform")
        session.commit()

    def cancelled():
        with sessions() as session:
            current = session.get(TerraformExecution, instance_id)
            tenant = session.get(Tenant, instance.tenant_id)
            if tenant is None or not tenant.active:
                raise PlanError("TENANT_INACTIVE")
            return current.cancel_requested or current.attempt != number

    error_code, result = None, None
    try:
        if cancelled():
            raise PlanError("CANCELLED")
        executor = executor or build_executor()
        with sessions() as session:
            execution = session.get(TerraformExecution, instance_id)
            if execution.source_revision not in {None, SOURCE_REVISION}:
                raise PlanError("SOURCE_REVISION_CHANGED")
            allocation = Allocation.model_validate(execution.allocation) if execution.allocation else load_allocation(instance)
            if instance.network_profile:
                from .profiles import load_profile, settings_for
                profile=load_profile(instance.network_profile,instance.tenant_id)
                actual=allocation.model_dump(exclude={'vm_id','ipv4_cidr','ipv4_gateway'})
                if actual != settings_for(profile, allocation.node_name).model_dump():
                    raise PlanError('PROFILE_CONFIGURATION_CHANGED')
            variables = allocation.variables(instance)
            if instance.self_service_policy_id or instance.commercial_revision:
                from ..services.lab import member
                member(session,instance,"create",current_profiles=True)
            execution.allocation = allocation.model_dump()
            execution.vmid, execution.source_revision = allocation.vm_id, SOURCE_REVISION
            state_key = execution.state_key
            try:
                session.commit()
            except IntegrityError:
                raise PlanError("VMID_ALREADY_RESERVED") from None
        result = executor.plan(instance_id, number, state_key, allocation, variables, cancelled)
        executor.prepare_apply(instance_id, number, result.sha256, state_key, allocation.model_dump(), variables)
    except PlanError as error:
        error_code = error.code
    except Conflict as error:
        error_code = str(error)
    except Exception:
        error_code = "TERRAFORM_EXECUTION_FAILED"
    with sessions() as session:
        execution = session.get(TerraformExecution, instance_id, with_for_update=True)
        if execution.attempt != number:
            return
        job = session.get(Job, job_id)
        operation = session.get(Operation, execution.operation_id)
        instance = session.get(Instance, instance_id)
        attempt = session.scalar(select(TerraformAttempt).where(TerraformAttempt.instance_id == instance_id, TerraformAttempt.number == number))
        if execution.cancel_requested:
            error_code = "CANCELLED"
        status = "cancelled" if error_code == "CANCELLED" else "failed" if error_code else "planned"
        execution.status, execution.error_code = status, error_code
        execution.plan_sha256 = result.sha256 if not error_code else None
        attempt.status, attempt.error_code, attempt.finished_at = status, error_code, utcnow()
        attempt.plan_sha256 = execution.plan_sha256
        job.status = "failed" if error_code else "completed"
        job.message = "Planificación fallida; consulte la ejecución Terraform" if error_code else "Plan verificado; apply deshabilitado y aprobación pendiente"
        operation.status, operation.error_code = ("failed" if error_code else "running"), error_code
        instance.status = "failed" if error_code and error_code != "CANCELLED" else "pending"
        if error_code:
            instance.result = None
        if not error_code:
            instance.result = {"phase": "awaiting_approval", "plan_sha256": result.sha256, **result.summary}
        event(session, operation, "terraform_" + status, "worker:terraform")
        session.commit()
