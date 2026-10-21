from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from ..executors import ExecutionRequest, ExecutorUnavailable, IaCExecutor, get_executor
from ..models import Instance, Job, Operation, Tenant
from ..schemas import InstanceCreate
from .instances import event


def execute_instance(job_id: str, sessions: sessionmaker[Session], executor: IaCExecutor | None = None) -> None:
    with sessions() as session:
        claimed = session.execute(update(Job).where(
            Job.id == job_id, Job.operation == "instance.create", Job.status == "queued",
            Job.operation_id.is_not(None),
        ).values(status="running")).rowcount
        if not claimed:
            session.rollback()
            return
        job = session.get(Job, job_id)
        operation = session.get(Operation, job.operation_id)
        instance = session.get(Instance, operation.instance_id)
        if (job.tenant_id != operation.tenant_id or instance.tenant_id != operation.tenant_id
                or operation.status != "queued" or instance.status != "pending"):
            # No ejecutar asociaciones inconsistentes aunque el mensaje contenga un ID válido.
            session.rollback()
            return
        operation.status = "running"
        instance.status = "provisioning"
        event(session, operation, "started", "worker:simulated")
        spec = InstanceCreate.model_validate({key: getattr(instance, key) for key in InstanceCreate.model_fields})
        request = ExecutionRequest(instance.id, operation.id, operation.tenant_id, spec)
        tenant = session.get(Tenant, operation.tenant_id)
        allowed = tenant is not None and tenant.active
        session.commit()
    error_code = None
    try:
        if not allowed:
            error_code = "TENANT_INACTIVE"
        else:
            result = (executor or get_executor()).create(request)
    except ExecutorUnavailable:
        error_code = "EXECUTOR_UNAVAILABLE"
    except Exception:
        # Los detalles del proveedor pueden contener secretos; persistir solo un código estable.
        error_code = "EXECUTION_FAILED"
    with sessions() as session:
        job = session.get(Job, job_id)
        operation = session.get(Operation, request.operation_id)
        instance = session.get(Instance, request.instance_id)
        job.status = operation.status = "failed" if error_code else "completed"
        operation.error_code = error_code
        instance.status = "failed" if error_code else "ready"
        job.message = "Falló la ejecución simulada; consulte la operación" if error_code else "Simulación completada; Terraform NO fue ejecutado."
        if not error_code:
            instance.result = result.details
        event(session, operation, "execution_failed" if error_code else "completed", "worker:simulated")
        session.commit()
