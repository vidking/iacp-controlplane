from sqlalchemy import select

from ..models import Instance, Job, Operation, TerraformAttempt, TerraformExecution, utcnow
from ..services.instances import Conflict, event
from ..services.jobs import QueueUnavailable
from .lifecycle import fail_dispatch
from .locks import ExecutionBusy, instance_lock


def cancel(session, execution, actor):
    execution = session.get(TerraformExecution, execution.instance_id, with_for_update=True, populate_existing=True)
    from .apply import block_replan
    block_replan(session, execution.instance_id)
    if execution.status in {"cancelled", "failed"} or execution.cancel_requested:
        return
    execution.cancel_requested = True
    operation = session.get(Operation, execution.operation_id)
    if execution.status == "running":
        event(session, operation, "terraform_cancel_requested", actor)
    else:
        execution.status, execution.plan_sha256, execution.error_code = "cancelled", None, "CANCELLED"
        attempt = session.scalar(select(TerraformAttempt).where(TerraformAttempt.instance_id == execution.instance_id,
                                                                 TerraformAttempt.number == execution.attempt))
        attempt.status, attempt.error_code, attempt.finished_at = "cancelled", "CANCELLED", utcnow()
        job = session.get(Job, execution.job_id)
        job.status, job.message = "failed", "Plan cancelado; apply no fue ejecutado"
        operation.status, operation.error_code = "failed", "CANCELLED"
        instance = session.get(Instance, execution.instance_id)
        instance.status, instance.result = "pending", None
        event(session, operation, "terraform_cancelled", actor)
    session.commit()


def queue_response(execution, number):
    return {"instance_id": execution.instance_id, "job_id": execution.job_id,
            "operation_id": execution.operation_id, "attempt": number, "status": "queued"}


def retry(session, execution, expected, key, actor, publish):
    previous = session.scalar(select(TerraformAttempt).where(TerraformAttempt.instance_id == execution.instance_id,
                                                            TerraformAttempt.retry_key == key))
    if previous is not None:
        if previous.number != expected + 1:
            raise Conflict("Retry key already used for another attempt")
        return queue_response(execution, previous.number)
    try:
        with instance_lock(session.get_bind(), execution.instance_id):
            execution = session.get(TerraformExecution, execution.instance_id, with_for_update=True, populate_existing=True)
            from .apply import block_replan
            block_replan(session, execution.instance_id)
            # Releer después del lock: otra petición pudo registrar esta misma clave.
            previous = session.scalar(select(TerraformAttempt).where(TerraformAttempt.instance_id == execution.instance_id,
                                                                    TerraformAttempt.retry_key == key))
            if previous is not None:
                if previous.number != expected + 1:
                    raise Conflict("Retry key already used for another attempt")
                return queue_response(execution, previous.number)
            if execution.attempt != expected or execution.status == "planned":
                raise Conflict("Attempt changed or plan already prepared; inspect the current execution")
            old = session.scalar(select(TerraformAttempt).where(TerraformAttempt.instance_id == execution.instance_id,
                                                               TerraformAttempt.number == execution.attempt))
            operation = session.get(Operation, execution.operation_id)
            if execution.status in {"queued", "running"}:
                old.status, old.error_code, old.finished_at = "interrupted", "INTERRUPTED", utcnow()
                operation.status = "failed"
                event(session, operation, "terraform_interrupted", actor)
            execution.attempt += 1
            execution.status, execution.error_code, execution.plan_sha256 = "queued", None, None
            execution.cancel_requested = False
            session.add(TerraformAttempt(instance_id=execution.instance_id, number=execution.attempt,
                                        retry_key=key, status="queued", actor=actor))
            job = session.get(Job, execution.job_id)
            job.status, job.message = "queued", ""
            operation.status, operation.error_code = "queued", None
            instance = session.get(Instance, execution.instance_id)
            instance.status, instance.result = "pending", None
            event(session, operation, "terraform_retry_queued", actor)
            response = queue_response(execution, execution.attempt)
            session.commit()
    except ExecutionBusy:
        raise Conflict("An active worker holds the instance lock") from None
    # Liberar el lock antes de publicar: un worker rápido debe poder reclamarlo.
    try:
        publish(execution.job_id, response["attempt"])
    except Exception:
        execution = session.get(TerraformExecution, execution.instance_id, with_for_update=True, populate_existing=True)
        if execution.attempt == response["attempt"] and execution.status == "queued":
            fail_dispatch(session, execution)
            job = session.get(Job, execution.job_id)
            operation = session.get(Operation, execution.operation_id)
            job.status, operation.status, operation.error_code = "failed", "failed", "DISPATCH_FAILED"
            session.get(Instance, execution.instance_id).status = "failed"
            event(session, operation, "terraform_dispatch_failed", actor)
        session.commit()
        raise QueueUnavailable from None
    return response
