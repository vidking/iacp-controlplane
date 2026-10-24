"""Disposable identities only; this module never connects to a real hypervisor."""
from uuid import uuid4

from iacp.models import Instance, Job, Operation, TerraformApply, TerraformExecution, utcnow
from iacp.lifecycle.proxmox import readiness
from iacp.terraform.config import SOURCE_REVISION


def managed_instance(sessions, tenant_id, *, vmid=21001, name="fixture-kvm"):
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
        session.add(TerraformExecution(instance_id=identifier, job_id=job_id, operation_id=operation_id,
            status="planned", attempt=1, state_key="synthetic/" + identifier, vmid=vmid,
            plan_sha256="a" * 64, source_revision=SOURCE_REVISION))
        session.flush()
        session.add(TerraformApply(id=approval_id, instance_id=identifier, attempt=1, plan_sha256="a" * 64,
            approval_key="synthetic-approval", request_hash="a" * 64, approved_by="synthetic-reviewer",
            expires_at=utcnow(), status="completed", result=result, started_at=utcnow(), finished_at=utcnow()))
        session.commit()
    return identifier


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
