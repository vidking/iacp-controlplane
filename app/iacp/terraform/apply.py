"""Autorizaciones y aplicación de planes: persistencia independiente de HTTP."""
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import os
import time
from uuid import uuid4

from sqlalchemy import select

from ..models import Instance, Job, Operation, TerraformApply, TerraformExecution, Tenant, utcnow
from ..services.instances import Conflict, event
from ..services.jobs import QueueUnavailable
from .config import Allocation, PlanError
from ..services.ownership_binding import valid_source_revision
from .lifecycle import build_executor
from .locks import ExecutionBusy, instance_lock


def enabled():
    return os.environ.get("IACP_TERRAFORM_APPLY_ENABLED") == "1"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _executor_revision_call(method, *args, revision):
    """Pasa la revisión sellada sin romper ejecutores de observación antiguos."""
    parameters = inspect.signature(method).parameters.values()
    if "revision" in inspect.signature(method).parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    ):
        return method(*args, revision=revision)
    return method(*args)


def not_expired(value):
    return value.replace(tzinfo=timezone.utc) > utcnow()


def block_replan(session, instance_id):
    if session.scalar(select(TerraformApply.id).where(TerraformApply.instance_id == instance_id,
            TerraformApply.status.in_(["approved", "queued", "running", "completed", "uncertain"]))) is not None:
        raise Conflict("APPLY_REQUIRES_RECONCILIATION")


def approve(session, execution, body, key, principal):
    execution = session.get(TerraformExecution, execution.instance_id, with_for_update=True, populate_existing=True)
    previous = session.scalar(select(TerraformApply).where(TerraformApply.instance_id == execution.instance_id,
                                                         TerraformApply.approval_key == key))
    digest = fingerprint(body)
    if previous is not None:
        if previous.request_hash != digest or previous.approved_by != principal.subject:
            raise Conflict("IDEMPOTENCY_CONFLICT")
        return previous
    operation = session.get(Operation, execution.operation_id)
    if principal.subject == operation.requested_by:
        raise Conflict("INDEPENDENT_APPROVAL_REQUIRED")
    if (execution.status != "planned" or execution.cancel_requested or execution.attempt != body["attempt"]
            or execution.plan_sha256 != body["plan_sha256"]
            or not valid_source_revision(execution.source_revision)):
        raise Conflict("PLAN_CHANGED")
    if session.scalar(select(TerraformApply.id).where(TerraformApply.instance_id == execution.instance_id,
                                                     TerraformApply.attempt == execution.attempt)):
        raise Conflict("PLAN_ALREADY_AUTHORIZED")
    block_replan(session, execution.instance_id)
    result = TerraformApply(id=str(uuid4()), instance_id=execution.instance_id, attempt=execution.attempt,
                            plan_sha256=execution.plan_sha256, approval_key=key, request_hash=digest,
                            approved_by=principal.subject, expires_at=utcnow() + timedelta(seconds=body["valid_for_seconds"]),
                            status="approved")
    session.add(result)
    event(session, operation, "terraform_apply_approved", principal.subject)
    session.commit()
    return result


def owned_apply(session, execution, identifier, lock=False):
    row = session.get(TerraformApply, identifier, with_for_update=lock, populate_existing=lock)
    if row is None or row.instance_id != execution.instance_id:
        raise Conflict("APPROVAL_NOT_FOUND")
    return row


def dispatch(session, execution, identifier, key, principal, publish):
    execution = session.get(TerraformExecution, execution.instance_id, with_for_update=True, populate_existing=True)
    row = owned_apply(session, execution, identifier)
    if row.dispatch_key is not None:
        if row.dispatch_key != key or row.requested_by != principal.subject:
            raise Conflict("IDEMPOTENCY_CONFLICT")
        return row
    if not enabled():
        raise PlanError("APPLY_DISABLED")
    if row.status != "approved" or row.started_at or not not_expired(row.expires_at):
        raise Conflict("APPROVAL_EXPIRED_OR_CONSUMED")
    if row.approved_by == principal.subject:
        raise Conflict("INDEPENDENT_APPROVAL_REQUIRED")
    check_binding(row, execution)
    row.dispatch_key, row.requested_by, row.status = key, principal.subject, "queued"
    job = Job(id=str(uuid4()), operation="instance.apply", tenant_id=principal.tenant_id, status="queued")
    session.add(job)
    session.flush()
    row.job_id = job.id
    operation = session.get(Operation, execution.operation_id)
    event(session, operation, "terraform_apply_queued", principal.subject)
    session.commit()
    try:
        publish(row.id, "apply")
    except Exception:
        # La publicación pudo llegar. Conservar queued permite reclamar una sola vez;
        # recuperar explícitamente el mismo run no crea un segundo apply.
        event(session, operation, "terraform_apply_dispatch_unconfirmed", principal.subject)
        session.commit()
        raise QueueUnavailable from None
    return row


def check_binding(row, execution):
    if (row.instance_id != execution.instance_id or row.attempt != execution.attempt or row.plan_sha256 != execution.plan_sha256
            or execution.status != "planned" or execution.cancel_requested
            or not valid_source_revision(execution.source_revision)):
        raise PlanError("PLAN_CHANGED")


def cancel_apply(session, execution, identifier, principal):
    session.get(TerraformExecution, execution.instance_id, with_for_update=True, populate_existing=True)
    row = owned_apply(session, execution, identifier, lock=True)
    if row.status == "completed":
        raise Conflict("APPLY_ALREADY_COMPLETED")
    if row.status == "cancelled" or row.error_code == "RESOURCE_ABSENCE_CONFIRMED":
        return row
    # Si ya cruzó el límite de mutación, la cancelación requiere conciliación.
    row.status = "uncertain" if row.started_at else "cancelled"
    row.error_code = "RECONCILIATION_REQUIRED" if row.started_at else "CANCELLED"
    if row.job_id:
        session.get(Job, row.job_id).status = "failed"
    operation = session.get(Operation, execution.operation_id)
    operation.status, operation.error_code = "failed", row.error_code
    if row.started_at:
        instance = session.get(Instance, row.instance_id)
        instance.status = "failed"
        instance.result = {"simulated": False, "phase": "uncertain", "error_code": row.error_code,
                           "apply_id": row.id}
    event(session, operation, "terraform_apply_cancel_requested", principal.subject)
    session.commit()
    return row


def recover_apply(session, execution, identifier, principal, publish):
    """Cercar entregas apply pendientes antes de publicar exclusivamente observación."""
    try:
        with instance_lock(session.get_bind(), execution.instance_id):
            session.get(TerraformExecution, execution.instance_id, with_for_update=True, populate_existing=True)
            row = owned_apply(session, execution, identifier, lock=True)
            if row.status not in {"queued", "running", "uncertain"}:
                raise Conflict("RECOVERY_NOT_REQUIRED")
            if row.status == "queued":
                row.status, row.error_code = "uncertain", "RECONCILIATION_REQUIRED"
            event(session, session.get(Operation, execution.operation_id), "terraform_recovery_requested", principal.subject)
            session.commit()
    except ExecutionBusy:
        raise Conflict("An active worker holds the instance lock") from None
    try:
        publish(row.id, "reconcile")
    except Exception:
        raise QueueUnavailable from None
    return row


def resolve_absence(session, execution, identifier, body, key, principal):
    """Registrar una atestación independiente; no consulta ni modifica infraestructura."""
    try:
        with instance_lock(session.get_bind(), execution.instance_id):
            execution = session.get(TerraformExecution, execution.instance_id, with_for_update=True, populate_existing=True)
            row = owned_apply(session, execution, identifier, lock=True)
            previous = (row.result or {}).get("resolution")
            digest = fingerprint(body)
            if previous:
                if (previous["key"] != key or previous["request_sha256"] != digest
                        or previous["actor"] != principal.subject):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return row
            operation = session.get(Operation, execution.operation_id)
            if principal.subject in {operation.requested_by, row.requested_by}:
                raise Conflict("INDEPENDENT_APPROVAL_REQUIRED")
            if (row.status != "uncertain" or row.error_code != "RESOURCE_ABSENT"
                    or (row.result or {}).get("reconciliation", {}).get("plan_sha256") != row.plan_sha256
                    or (row.result or {}).get("diagnostic", {}).get("state_present") is not False
                    or (row.result or {}).get("diagnostic", {}).get("proxmox_present") is not False):
                raise Conflict("ABSENCE_NOT_CONFIRMED")
            check_binding(row, execution)
            allocation = Allocation.model_validate(execution.allocation)
            if (body["attempt"] != row.attempt or body["plan_sha256"] != row.plan_sha256
                    or body["state"]["bucket"] != allocation.backend.bucket
                    or body["state"]["key"] != execution.state_key
                    or body["proxmox"]["vmid"] != allocation.vm_id
                    or body["proxmox"]["node"] != allocation.node_name):
                raise Conflict("PLAN_CHANGED")
            for evidence in (body["state"], body["proxmox"]):
                observed = datetime.fromisoformat(evidence["observed_at"])
                if observed < (row.started_at or row.created_at).replace(tzinfo=timezone.utc) or observed > utcnow():
                    raise Conflict("INVALID_ABSENCE_EVIDENCE")
            resolution = {"kind": "confirmed_absence", "key": key, "request_sha256": digest,
                          "actor": principal.subject, "recorded_at": utcnow().isoformat(), "evidence": body}
            row.status, row.error_code, row.finished_at = "failed", "RESOURCE_ABSENCE_CONFIRMED", utcnow()
            row.result = {**row.result, "resolution": resolution}
            job = session.get(Job, row.job_id)
            job.status, job.message = "failed", "Ausencia confirmada mediante evidencia revisada; aprovisionamiento fallido"
            operation.status, operation.error_code = "failed", row.error_code
            instance = session.get(Instance, row.instance_id)
            instance.status, instance.vmid, instance.node = "failed", None, None
            instance.result = {"simulated": False, "phase": "resolved_absent", "error_code": row.error_code,
                               "apply_id": row.id, "resolution": resolution}
            event(session, operation, "terraform_absence_resolved", principal.subject)
            session.commit()
            return row
    except ExecutionBusy:
        raise Conflict("An active worker holds the instance lock") from None


def execute_apply(identifier, sessions, executor=None, reconcile=False):
    with sessions() as session:
        row = session.get(TerraformApply, identifier)
        if row is None:
            return
        instance_id = row.instance_id
    try:
        with instance_lock(sessions.kw["bind"], instance_id) as heartbeat:
            _execute(identifier, sessions, executor, reconcile, heartbeat)
    except ExecutionBusy:
        return


def _execute(identifier, sessions, executor, reconcile, heartbeat):
    with sessions() as session:
        row = session.get(TerraformApply, identifier, with_for_update=True)
        if (not reconcile and (row.status != "queued" or row.started_at)) or (reconcile and row.status not in {"running", "uncertain"}):
            return
        execution = session.get(TerraformExecution, row.instance_id)
        instance = session.get(Instance, row.instance_id)
        operation = session.get(Operation, execution.operation_id)
        row.status = "running"
        job = session.get(Job, row.job_id)
        job.status = "running"
        event(session, operation, "terraform_reconcile_started" if reconcile else "terraform_apply_started", "worker:terraform")
        session.commit()
        number, sha, state_key = row.attempt, row.plan_sha256, execution.state_key
        allocation_data, source_revision = execution.allocation, execution.source_revision

    deadline = time.monotonic() + 900
    def cancelled():
        heartbeat()
        if time.monotonic() >= deadline:
            raise PlanError("EXECUTION_DEADLINE_EXCEEDED")
        with sessions() as session:
            current = session.get(TerraformApply, identifier)
            tenant = session.get(Tenant, instance.tenant_id)
            if not tenant or not tenant.active:
                raise PlanError("TENANT_INACTIVE")
            return current.status != "running" or (not reconcile and not enabled())

    def before_mutation():
        if cancelled():
            raise PlanError("CANCELLED")
        with sessions() as session:
            current = session.get(TerraformApply, identifier, with_for_update=True)
            from ..services.authorization import member
            try:
                member(session, session.get(Instance, current.instance_id), "create", current_profiles=True)
            except Conflict as failure:
                raise PlanError(str(failure)) from None
            check_binding(current, session.get(TerraformExecution, current.instance_id))
            if current.status != "running" or current.started_at or not not_expired(current.expires_at):
                raise PlanError("APPROVAL_EXPIRED_OR_CONSUMED")
            current.started_at = utcnow()
            session.get(Instance, current.instance_id).status = "provisioning"
            event(session, session.get(Operation, execution.operation_id), "terraform_mutation_started", "worker:terraform")
            session.commit()

    result, error, diagnostic, reconciliation = None, None, None, None
    try:
        if cancelled():
            raise PlanError("CANCELLED")
        if not reconcile and not enabled():
            raise PlanError("APPLY_DISABLED")
        executor = executor or (build_executor(observation=True) if reconcile else build_executor())
        allocation = Allocation.model_validate(allocation_data)
        variables = allocation.variables(instance)
        with sessions() as session:
            current = session.get(TerraformApply, identifier)
            check_binding(current, session.get(TerraformExecution, current.instance_id))
            if not reconcile and not not_expired(current.expires_at):
                raise PlanError("APPROVAL_EXPIRED_OR_CONSUMED")
        if reconcile:
            reconciliation = _executor_revision_call(
                executor.prepare_reconcile, instance.id, number, sha, state_key, allocation_data, variables,
                revision=source_revision,
            )
            result = executor.observe(instance.id, number, allocation_data, variables, cancelled)
        else:
            _executor_revision_call(
                executor.prepare_apply, instance.id, number, sha, state_key, allocation_data, variables,
                revision=source_revision,
            )
            result = _executor_revision_call(
                executor.apply, instance.id, number, sha, state_key, allocation_data, variables,
                cancelled, before_mutation, revision=source_revision,
            )
        # Defensa del contrato incluso ante otra implementación del ejecutor.
        if (result.get("simulated") is not False or result.get("vmid") != allocation.vm_id
                or result.get("node") != allocation.node_name):
            raise PlanError("OBSERVED_RESOURCE_MISMATCH")
        observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
        if any(observation.get(key) is not True
               for key in ("state_present", "proxmox_present", "postconditions_verified")):
            # Un apply o una reconciliación sin evidencia física completa no
            # puede cerrar el fence ni materializar ownership técnico.
            raise PlanError("TECHNICAL_OWNERSHIP_EVIDENCE_REQUIRED")
        if reconcile:
            # Revalidar el vínculo aprobado tras la observación, antes del commit.
            _executor_revision_call(
                executor.prepare_reconcile, instance.id, number, sha, state_key, allocation_data, variables,
                revision=source_revision,
            )
        if cancelled():
            raise PlanError("CANCELLED")
    except PlanError as failure:
        error = failure.code
        diagnostic = failure.diagnostic()
    except Exception:
        error = "TERRAFORM_EXECUTION_FAILED"
    # Si se perdió la conexión que mantiene el advisory lock, no publicar un
    # resultado desde un worker que ya no posee la instancia. /recover reevaluará.
    heartbeat()
    with sessions() as session:
        current_execution = session.get(TerraformExecution, instance.id, with_for_update=True)
        row = session.get(TerraformApply, identifier, with_for_update=True)
        if row.status != "running":
            return
        try:
            check_binding(row, current_execution)
        except PlanError as failure:
            error, diagnostic = failure.code, failure.diagnostic()
        job = session.get(Job, row.job_id)
        operation = session.get(Operation, execution.operation_id)
        instance = session.get(Instance, row.instance_id)
        row.status = ("uncertain" if error and (row.started_at or reconcile) else
                      "cancelled" if error and row.status == "cancelled" else "failed" if error else "completed")
        row.error_code, row.result, row.finished_at = error, result if not error else ({"diagnostic": diagnostic} if diagnostic else None), utcnow()
        if reconciliation is not None:
            row.result = {**(row.result or {}), "reconciliation": {**reconciliation,
                          "approval_id": row.id, "instance_id": row.instance_id, "attempt": row.attempt,
                          "operation_id": operation.id, "observed_at": utcnow().isoformat()}}
        job.status = "failed" if error else "completed"
        job.message = "Consulte la autorización y los eventos Terraform" if error else "KVM observada mediante el provider"
        operation.status, operation.error_code = ("failed" if error else "completed"), error
        if not error:
            instance.status, instance.vmid, instance.node = "ready", result["vmid"], result["node"]
            instance.result = {**result, "phase": "observed", "observed_at": utcnow().isoformat(), "apply_id": row.id}
            # El vínculo técnico queda materializado al terminar el apply.
            # Lifecycle, red y borrado solo lo vuelven a validar; no deben ser
            # el primer lugar donde se adopta una KVM ya creada.
            from ..services.technical_ownership import record as technical_record
            technical_record(session, instance, adopt=True)
        else:
            instance.status = "failed"
            instance.result = {"simulated": False, "phase": row.status, "error_code": error,
                               "apply_id": row.id, **({"diagnostic": diagnostic} if diagnostic else {})}
        event(session, operation, "terraform_apply_" + row.status, "worker:terraform")
        heartbeat()
        session.commit()
