"""Disposable identities only; this module never connects to a real hypervisor."""
from uuid import uuid4
from sqlalchemy import select

from iacp.models import (Instance, Job, Operation, TechnicalOwnership, TerraformApply,
                         TerraformExecution, utcnow)
from iacp.lifecycle.proxmox import readiness
from iacp.services.ownership_binding import binding_digest
from iacp.terraform.config import SOURCE_REVISION


def managed_instance(sessions, tenant_id, *, vmid=30001, name="fixture-kvm"):
    identifier, operation_id, job_id, approval_id = (str(uuid4()) for _ in range(4))
    result = {"simulated": False, "vmid": vmid, "node": "mng01", "name": name,
              "observation": {"postconditions_verified": True, "state_present": True, "proxmox_present": True}}
    with sessions() as session:
        instance = Instance(id=identifier, tenant_id=tenant_id, name=name, image="synthetic-image", vcpus=2,
                             memory_mb=2048, disk_gb=20, status="ready", executor="terraform", vmid=vmid,
                             node="mng01", result=result)
        from sqlalchemy import inspect, MetaData, Table
        if set(Instance.__table__.c.keys()) <= {c["name"] for c in inspect(session.connection()).get_columns("instances")}:
            session.add(instance)
        else:
            table = Table("instances", MetaData(), autoload_with=session.connection())
            values = {c.name: getattr(instance,c.name) for c in table.c if getattr(instance,c.name) is not None}
            values.update(created_at=utcnow(), updated_at=utcnow())
            session.execute(table.insert().values(**values))
        session.flush()
        operation = Operation(id=operation_id, tenant_id=tenant_id, instance_id=identifier,
            kind="instance.create", status="completed", idempotency_key="seed-" + identifier,
            request_hash="a" * 64, requested_by="bootstrap")
        if 'correlation_id' in {c['name'] for c in inspect(session.connection()).get_columns('operations')}:
            session.add(operation)
        else:
            table = Table('operations', MetaData(), autoload_with=session.connection())
            values = {c.name: getattr(operation,c.name) for c in table.c if getattr(operation,c.name) is not None}
            values.update(created_at=utcnow(), updated_at=utcnow())
            session.execute(table.insert().values(**values))
        session.flush()
        session.add(Job(id=job_id, tenant_id=tenant_id, operation_id=operation_id, operation="instance.plan", status="completed"))
        session.flush()
        execution = TerraformExecution(instance_id=identifier, job_id=job_id, operation_id=operation_id,
            status="planned", attempt=1, state_key="synthetic/" + identifier, vmid=vmid,
            allocation={"image": "synthetic-image", "node_name": "mng01", "vm_id": vmid},
            plan_sha256="a" * 64, source_revision=SOURCE_REVISION)
        session.add(execution)
        session.flush()
        apply = TerraformApply(id=approval_id, instance_id=identifier, attempt=1, plan_sha256="a" * 64,
            approval_key="synthetic-approval", request_hash="a" * 64, approved_by="synthetic-reviewer",
            expires_at=utcnow(), status="completed", result=result, started_at=utcnow(), finished_at=utcnow())
        session.add(apply)
        session.flush()
        if ("technical_ownership" in inspect(session.connection()).get_table_names()
                and vmid not in {104, 106, 29000}):
            session.add(TechnicalOwnership(
                instance_id=identifier, tenant_id=tenant_id, vmid=vmid, node="mng01", name=name,
                state_key=execution.state_key, source_revision=execution.source_revision,
                binding_sha256=binding_digest(instance, execution, apply),
                allocation=execution.allocation, evidence={"apply_id": apply.id, "attempt": apply.attempt,
                    "plan_sha256": apply.plan_sha256}, verified_at=utcnow()))
        session.commit()
    return identifier


def refresh_technical_ownership(session, identifier):
    """Actualiza el fixture tras cambios intencionales de su identidad sellada."""
    from sqlalchemy import inspect

    if "technical_ownership" not in inspect(session.connection()).get_table_names():
        return
    ownership = session.get(TechnicalOwnership, identifier)
    if ownership is None:
        return
    instance = session.get(Instance, identifier)
    execution = session.get(TerraformExecution, identifier)
    apply = session.scalar(select(TerraformApply).where(
        TerraformApply.instance_id == identifier,
        TerraformApply.attempt == execution.attempt,
        TerraformApply.plan_sha256 == execution.plan_sha256,
    ))
    ownership.tenant_id = instance.tenant_id
    ownership.vmid = execution.vmid
    ownership.node = instance.node
    ownership.name = instance.name
    ownership.state_key = execution.state_key
    ownership.source_revision = execution.source_revision
    ownership.allocation = execution.allocation
    ownership.binding_sha256 = binding_digest(instance, execution, apply)


class FakePower:
    def __init__(self, power="stopped"):
        self.power = power
        self.calls = []
        self.outcome = "completed"

    def permissions(self, target, *, mutate=False):
        self.calls.append(("permissions", target, mutate))

    def observe(self, target, *, preflight=False):
        self.calls.append(("observe", target, preflight))
        return readiness(self.power)

    def submit(self, target, action):
        self.calls.append(("submit", target, action))
        self.power = "stopped" if action == "shutdown" else "running"
        task_type = {"start": "qmstart", "shutdown": "qmshutdown", "reboot": "qmreboot"}[action]
        return f"UPID:{target.node}:00000001:00000002:00000003:{task_type}:{target.vmid}:synthetic@pve:"

    def task_status(self, target, action, task):
        self.calls.append(("task", target, action))
        return self.outcome
