"""At most one imperative dispatch; redelivery only monitors a consumed command."""
import time

from sqlalchemy import select

from ..models import Job, Operation, PowerExecution, Tenant, utcnow
from ..services.instances import event
from ..terraform.locks import ExecutionBusy, instance_lock
from .ownership import mutation_revision
from .proxmox import PowerError, ProxmoxPower
from .service import ACTIVE, binding


def execute_power(identifier, sessions, client=None, *, monitor_seconds=90, pause=time.sleep):
    with sessions() as session:
        instance_id = session.scalar(select(PowerExecution.instance_id).where(PowerExecution.operation_id == identifier))
    if instance_id is None:
        return
    try:
        with instance_lock(sessions.kw["bind"], instance_id) as heartbeat:
            _execute(identifier, sessions, client, heartbeat, monitor_seconds, pause)
    except ExecutionBusy:
        return


def _execute(identifier, sessions, client, heartbeat, monitor_seconds, pause):
    observation, task_outcome, error = None, None, None
    try:
        with sessions() as session:
            row = session.get(PowerExecution, identifier, with_for_update=True)
            if row.status not in ACTIVE:
                return
            operation = session.get(Operation, identifier, with_for_update=True)
            target, job = binding(session, row, operation)
            tenant = session.get(Tenant, operation.tenant_id)
            if tenant is None or not tenant.active:
                raise PowerError("TENANT_INACTIVE")
            if row.submitted_at is None and mutation_revision() != row.ownership_revision:
                raise PowerError("POWER_OWNERSHIP_UNAVAILABLE")
            if row.started_at is None:
                row.started_at = utcnow()
                operation.status = "running"
                event(session, operation, "power_started", "worker:lifecycle")
            row.status = operation.status = job.status = "running"
            row.phase = "preflight" if row.submitted_at is None else "monitoring"
            operation.error_code = None
            heartbeat()
            session.commit()
            action, submitted, task = row.action, row.submitted_at, row.task_id
        client = client or ProxmoxPower.configured()
        if submitted is None:
            client.permissions(target, mutate=True)
            observation = client.observe(target, preflight=True)
            goal = "stopped" if action == "shutdown" else "running"
            if action != "reboot" and observation["power_state"] == goal:
                observation = {**observation, "noop": True}
                task_outcome = "completed"
            else:
                if action == "reboot" and observation["power_state"] != "running":
                    raise PowerError("POWER_REBOOT_REQUIRES_RUNNING")
                # Consume the command BEFORE crossing the network boundary.
                # A crash between this commit and receiving the UPID is ambiguous
                # and must never issue the command again, particularly reboot.
                with sessions() as session:
                    row = session.get(PowerExecution, identifier, with_for_update=True)
                    operation = session.get(Operation, identifier)
                    binding(session, row, operation)
                    tenant = session.get(Tenant, operation.tenant_id)
                    if not tenant or not tenant.active:
                        raise PowerError("TENANT_INACTIVE")
                    if row.submitted_at is not None or row.status != "running":
                        raise PowerError("POWER_BINDING_CHANGED")
                    if mutation_revision() != row.ownership_revision:
                        raise PowerError("POWER_OWNERSHIP_UNAVAILABLE")
                    row.submitted_at, row.phase = utcnow(), "dispatching"
                    row.result = observation
                    event(session, operation, "power_dispatching", "worker:lifecycle")
                    heartbeat()
                    session.commit()
                heartbeat()
                task = client.submit(target, action)
                ProxmoxPower.validate_task(target, action, task)
                heartbeat()
                with sessions() as session:
                    row = session.get(PowerExecution, identifier, with_for_update=True)
                    if row.task_id is not None:
                        raise PowerError("POWER_BINDING_CHANGED")
                    row.task_id, row.phase = task, "monitoring"
                    event(session, session.get(Operation, identifier), "power_task_recorded", "worker:lifecycle")
                    heartbeat()
                    session.commit()
        elif task is None:
            raise PowerError("POWER_TASK_UNKNOWN")
        if task is not None:
            deadline = time.monotonic() + monitor_seconds
            while True:
                heartbeat()
                task_outcome = client.task_status(target, action, task)
                if task_outcome != "running":
                    break
                if time.monotonic() >= deadline:
                    raise PowerError("POWER_TASK_TIMEOUT")
                pause(min(2, max(0, deadline - time.monotonic())))
            observation = client.observe(target)
            if task_outcome == "timeout":
                raise PowerError("POWER_ACPI_TIMEOUT")
            if task_outcome != "completed":
                raise PowerError("POWER_TASK_FAILED")
            goal = "stopped" if action == "shutdown" else "running"
            # qmreboot can finish shutdown before qmeventd finishes starting the
            # replacement QEMU process. Observe within the same deadline; never
            # send a second power command to compensate for startup latency.
            while observation["power_state"] != goal and time.monotonic() < deadline:
                pause(min(2, max(0, deadline - time.monotonic())))
                heartbeat()
                observation = client.observe(target)
            if observation["power_state"] != goal:
                raise PowerError("POWER_RESULT_MISMATCH")
    except PowerError as failure:
        error = failure.code
    except Exception:
        error = "POWER_EXECUTION_FAILED"
    # Losing the session lock forbids a final commit from this worker.
    heartbeat()
    with sessions() as session:
        row = session.get(PowerExecution, identifier, with_for_update=True)
        operation = session.get(Operation, identifier, with_for_update=True)
        if row.status not in ACTIVE:
            return
        try:
            _, job = binding(session, row, operation)
            tenant = session.get(Tenant, operation.tenant_id)
            if not tenant or not tenant.active:
                raise PowerError("TENANT_INACTIVE")
        except PowerError as failure:
            error = failure.code
            job = session.scalar(select(Job).where(Job.operation_id == identifier))
        status = ("completed" if error is None else
                  "failed" if row.submitted_at is None or task_outcome in {"failed", "timeout"} else "uncertain")
        row.status = operation.status = status
        row.phase = "finished" if status in {"completed", "failed"} else "reconciliation_required"
        row.finished_at = utcnow() if status in {"completed", "failed"} else None
        if observation is not None:
            row.result = observation
        operation.error_code = error
        if job:
            job.status = "completed" if status == "completed" else "failed"
            job.message = "Consulte la operación Lifecycle"
        event(session, operation, "power_" + status, "worker:lifecycle")
        heartbeat()
        session.commit()
