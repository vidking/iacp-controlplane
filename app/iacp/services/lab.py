"""Tenant-scoped delegation. Policies never authorize adoption or uncertain retries."""
from datetime import timedelta, timezone
import os
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from ..models import Instance, LabPolicy, Operation, TerraformExecution, Tenant, utcnow
from ..security import Principal
from ..terraform.apply import fingerprint
from ..terraform.config import Allocation, SOURCE_REVISION
from ..terraform.locks import admission_lock
from .instances import Conflict, event


class LabPolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_extra={"examples":[{
        "environment":"lab","profiles":["lab-dhcp"],"actions":["create","start","shutdown","reboot","delete"],
        "max_instances":3,"max_vcpus":8,"max_memory_mb":8192,"max_disk_gb":80,
        "max_concurrent_operations":2,"valid_for_seconds":86400,"change_reference":"CAMBIO-LAB-REVISADO"}]})
    environment: str = Field(pattern="^lab$", default="lab")
    profiles: list[str] = Field(min_length=1, max_length=20)
    actions: list[str] = Field(min_length=1, max_length=5)
    max_instances: int = Field(ge=1, le=1000)
    max_vcpus: int = Field(ge=1, le=4096)
    max_memory_mb: int = Field(ge=512, le=1048576)
    max_disk_gb: int = Field(ge=8, le=1048576)
    max_concurrent_operations: int = Field(ge=1, le=32)
    valid_for_seconds: int = Field(default=86400, ge=300, le=604800)
    change_reference: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def bounded(self):
        if (len(set(self.profiles)) != len(self.profiles) or len(set(self.actions)) != len(self.actions)
                or "create" not in self.actions or not set(self.actions) <= {"create", "start", "shutdown", "reboot", "delete"}):
            raise ValueError("Explicit unique profiles and lab actions including create required")
        return self


def enabled():
    if os.environ.get("IACP_LAB_SELF_SERVICE_ENABLED", "0") != "1":
        raise Conflict("LAB_SELF_SERVICE_DISABLED")


def snapshots(spec, tenant_id):
    from ..terraform.profiles import load_profile
    result = {}
    for name in spec.profiles:
        p = load_profile(name, tenant_id)
        if (not p.self_service_enabled or p.settings.environment != "lab" or p.settings.network_mode != "dhcp"
                or p.settings.network_bridge != "vmbr0" or p.settings.vlan_id is not None
                or p.settings.started or p.settings.template_vm_id in {104, 106}):
            raise Conflict("LAB_PROFILE_NOT_ELIGIBLE")
        for requested, ceiling in (
            (spec.max_instances, p.max_live_instances), (spec.max_vcpus, p.max_total_vcpus),
            (spec.max_memory_mb, p.max_total_memory_mb), (spec.max_disk_gb, p.max_total_disk_gb),
            (spec.max_concurrent_operations, p.max_concurrent_operations)):
            if requested > ceiling:
                raise Conflict("LAB_POLICY_EXCEEDS_SERVER_LIMIT")
        result[name] = p.model_dump()
        if not p.root_password_bootstrap:
            result[name].pop('root_password_bootstrap')  # Preserve existing reviewed snapshots.
        if p.guest_initialization:
            from .guest_identity import PROGRAM_SHA256, ROOT_PROGRAM_SHA256
            result[name]['guest_initialization_program_sha256']=ROOT_PROGRAM_SHA256 if p.root_password_bootstrap else PROGRAM_SHA256
    return result


def digest_policy(tenant_id, spec, profiles):
    return fingerprint({"tenant_id": tenant_id, "specification": spec, "profiles": profiles, "source_revision": SOURCE_REVISION})


def record_review(row, action, actor):
    row.review_history=[*(row.review_history or []),{"action":action,"actor":actor,"at":utcnow().isoformat(),
        "policy_sha256":row.policy_sha256,"expires_at":row.expires_at.isoformat() if row.expires_at else None}]


def propose(session, principal, spec, key):
    with admission_lock(session):
        previous = session.scalar(select(LabPolicy).where(LabPolicy.tenant_id == principal.tenant_id, LabPolicy.idempotency_key == key))
        if previous:
            if previous.specification != spec.model_dump() or previous.requested_by != principal.subject:
                raise Conflict("IDEMPOTENCY_CONFLICT")
            return previous
        profiles = snapshots(spec, principal.tenant_id)
        row = LabPolicy(id=str(uuid4()), tenant_id=principal.tenant_id, idempotency_key=key,
            requested_by=principal.subject, status="proposed", specification=spec.model_dump(), profile_snapshots=profiles,
            policy_sha256=digest_policy(principal.tenant_id, spec.model_dump(), profiles))
        record_review(row,"proposed",principal.subject)
        session.add(row)
        session.commit()
        return row


def approve_policy(session, row, principal, sha):
    with admission_lock(session):
        row = session.get(LabPolicy, row.id, with_for_update=True, populate_existing=True)
        if principal.subject == row.requested_by:
            raise Conflict("INDEPENDENT_APPROVAL_REQUIRED")
        if row.policy_sha256 != sha:
            raise Conflict("LAB_POLICY_CHANGED")
        if row.status == "approved" and row.approved_by == principal.subject:
            return row
        if row.status != "proposed":
            raise Conflict("LAB_POLICY_NOT_PROPOSED")
        if snapshots(LabPolicySpec.model_validate(row.specification), row.tenant_id) != row.profile_snapshots:
            raise Conflict("LAB_PROFILE_CHANGED")
        row.approved_by, row.approved_at, row.status = principal.subject, utcnow(), "approved"
        row.expires_at = utcnow() + timedelta(seconds=row.specification["valid_for_seconds"])
        record_review(row,"approved",principal.subject)
        session.commit()
        return row


def renew_policy(session,row,principal,sha):
    with admission_lock(session):
        row=session.get(LabPolicy,row.id,with_for_update=True,populate_existing=True)
        if row.requested_by==principal.subject:
            raise Conflict("INDEPENDENT_APPROVAL_REQUIRED")
        if row.status!='approved' or row.policy_sha256!=sha:
            raise Conflict("LAB_POLICY_NOT_AUTHORIZED")
        if snapshots(LabPolicySpec.model_validate(row.specification),row.tenant_id)!=row.profile_snapshots:
            raise Conflict("LAB_PROFILE_CHANGED")
        row.expires_at=utcnow()+timedelta(seconds=row.specification['valid_for_seconds'])
        record_review(row,"renewed",principal.subject)
        session.commit()
        return row


def policy(session, identifier, tenant_id, action):
    enabled()
    row = session.get(LabPolicy, identifier, populate_existing=True)
    tenant = session.get(Tenant, tenant_id, populate_existing=True)
    if (row is None or row.tenant_id != tenant_id or row.status != "approved" or not row.approved_by
            or row.approved_by == row.requested_by or not row.approved_at or not row.expires_at
            or row.expires_at.replace(tzinfo=timezone.utc) <= utcnow() or not tenant or not tenant.active
            or action not in row.specification["actions"]
            or row.policy_sha256 != digest_policy(row.tenant_id, row.specification, row.profile_snapshots)):
        raise Conflict("LAB_POLICY_NOT_AUTHORIZED")
    return row


def usage(session, tenant_id, exclude_operation=None):
    # Failed and uncertain reservations remain charged until verified deletion.
    rows = session.scalars(select(Instance).where(Instance.tenant_id == tenant_id, Instance.status != "deleted")).all()
    active = session.scalars(select(Operation).where(Operation.tenant_id == tenant_id,
        Operation.status.in_(["queued", "running", "uncertain"]), Operation.kind != "instance.network.discover")).all()
    from ..models import TerraformApply
    active_ids={r.id for r in active}
    active_ids.update(session.scalars(select(TerraformExecution.operation_id).join(Instance)
        .join(TerraformApply, TerraformApply.instance_id==Instance.id).where(Instance.tenant_id==tenant_id,
            TerraformApply.status.in_(['approved','queued','running','uncertain']))))
    return {"instances": len(rows), "vcpus": sum(r.vcpus for r in rows),
        "memory_mb": sum(r.memory_mb for r in rows), "disk_gb": sum(r.disk_gb for r in rows),
        "concurrent_operations": len(active_ids - {exclude_operation})}


def check_quota(session, tenant_id, limits, instance=None, exclude_operation=None):
    used = usage(session, tenant_id, exclude_operation)
    requested = {"instances": 1 if instance else 0, "vcpus": instance.vcpus if instance else 0,
        "memory_mb": instance.memory_mb if instance else 0, "disk_gb": instance.disk_gb if instance else 0,
        "concurrent_operations": 1}
    for name, maximum in limits.items():
        if used[name] + requested[name] > maximum:
            raise Conflict("LAB_QUOTA_EXCEEDED:" + name)


def limits(row):
    return {"instances": row.specification["max_instances"], "vcpus": row.specification["max_vcpus"],
        "memory_mb": row.specification["max_memory_mb"], "disk_gb": row.specification["max_disk_gb"],
        "concurrent_operations": row.specification["max_concurrent_operations"]}


def instance_digest(instance, execution, row):
    values = {"instance_id": instance.id, "tenant_id": instance.tenant_id, "name": instance.name,
        "image": instance.image, "vcpus": instance.vcpus, "memory_mb": instance.memory_mb, "disk_gb": instance.disk_gb,
        "network_profile": instance.network_profile, "network_mode": instance.network_mode,
        "allocation": execution.allocation, "state_key": execution.state_key, "source_revision": execution.source_revision,
        "policy_id": row.id, "policy_sha256": row.policy_sha256}
    if instance.start_on_create:
        values['start_on_create'] = True
    if instance.initial_access_fingerprint:
        values['initial_access_fingerprint'] = instance.initial_access_fingerprint
    if instance.commercial_context:
        values['commercial_context'] = instance.commercial_context
    return fingerprint(values)


def admit(session, instance):
    row = policy(session, instance.self_service_policy_id, instance.tenant_id, "create")
    if instance.network_profile not in row.profile_snapshots:
        raise Conflict("LAB_PROFILE_NOT_AUTHORIZED")
    if snapshots(LabPolicySpec.model_validate(row.specification), row.tenant_id) != row.profile_snapshots:
        raise Conflict("LAB_PROFILE_CHANGED")
    check_quota(session, instance.tenant_id, limits(row), instance)
    return row


def member(session, instance, action, *, current_profiles=False):
    row = policy(session, instance.self_service_policy_id, instance.tenant_id, action)
    execution = session.get(TerraformExecution, instance.id)
    creation = session.scalar(select(Operation).where(Operation.instance_id == instance.id, Operation.kind == "instance.create"))
    if (execution is None or creation is None or creation.requested_by == row.approved_by
            or instance.created_at.replace(tzinfo=timezone.utc) < row.approved_at.replace(tzinfo=timezone.utc)
            or instance.network_profile not in row.profile_snapshots
            or execution.source_revision != SOURCE_REVISION or execution.vmid is None or execution.vmid < 30000
            or execution.vmid in {104, 106} or instance.policy_binding_sha256 != instance_digest(instance, execution, row)):
        raise Conflict("LAB_OWNERSHIP_BINDING_CHANGED")
    allocation = Allocation.model_validate(execution.allocation)
    if (allocation.network_mode != "dhcp" or allocation.network_bridge != "vmbr0" or allocation.vlan_id is not None
            or allocation.vm_id != execution.vmid):
        raise Conflict("LAB_OWNERSHIP_BINDING_CHANGED")
    if current_profiles and snapshots(LabPolicySpec.model_validate(row.specification), row.tenant_id) != row.profile_snapshots:
        raise Conflict("LAB_PROFILE_CHANGED")
    return row


def authorize_operation(session, instance, action):
    if instance.self_service_policy_id:
        row = member(session, instance, action)
        check_quota(session, instance.tenant_id, {"concurrent_operations": limits(row)["concurrent_operations"]})


def advance(session, instance, publish):
    """Approve the exact verified plan by delegation, then use the existing Apply fence."""
    from ..terraform.apply import approve, dispatch, recover_apply
    execution = session.get(TerraformExecution, instance.id)
    row = member(session, instance, "create", current_profiles=True)
    operation = session.get(Operation, execution.operation_id)
    principal = Principal(instance.tenant_id, operation.requested_by, "operator")
    from ..models import TerraformApply
    approval = session.scalar(select(TerraformApply).where(TerraformApply.instance_id == instance.id,
        TerraformApply.attempt == execution.attempt))
    if approval and approval.status in {"running", "uncertain"}:
        return recover_apply(session, execution, approval.id, principal, publish)
    if approval and approval.status == "completed":
        return approval
    if approval and approval.status == "queued" and not approval.started_at:
        publish(approval.id, "apply")
        return approval
    if execution.status != "planned":
        raise Conflict("LAB_PLAN_NOT_READY")
    if approval is None:
        delegate = Principal(instance.tenant_id, "policy:" + row.id, "approver")
        approval = approve(session, execution, {"attempt": execution.attempt, "plan_sha256": execution.plan_sha256,
            "valid_for_seconds": min(900, max(60, int((row.expires_at.replace(tzinfo=timezone.utc)-utcnow()).total_seconds())))},
            "policy:" + row.id + ":" + str(execution.attempt), delegate)
    return dispatch(session, execution, approval.id, "policy-apply:" + approval.id, principal, publish)


def advance_deletion(session, instance, operation_id, publish):
    from ..models import DeletionExecution
    from ..terraform import deletion
    row = session.get(DeletionExecution, operation_id, populate_existing=True)
    operation = session.get(Operation, operation_id)
    if row is None or not row.autonomous or row.status in {"failed", "completed"}:
        return
    principal = Principal(instance.tenant_id, operation.requested_by, "operator")
    if row.started_at:
        return deletion.recover_or_cancel(session, instance, principal, operation_id, publish)
    policy_row = member(session, instance, "delete", current_profiles=True)
    body = {"operation_id": row.operation_id, "plan_sha256": row.plan_sha256}
    if row.status == "planned":
        deletion.approve(session, instance, Principal(instance.tenant_id, "policy:" + policy_row.id, "approver"),
            {**body, "valid_for_seconds": 300}, "policy-delete:" + row.operation_id)
    if row.phase in {"approved", "delete_queued"}:
        return deletion.dispatch(session, instance, principal, body, operation.idempotency_key, publish)


def after_plan(job_id, sessions, publish):
    """A durable plan stays inspectable if policy/broker gates pause delegation."""
    with sessions() as session:
        execution = session.scalar(select(TerraformExecution).where(TerraformExecution.job_id == job_id))
        if execution is None or execution.status != "planned":
            return
        instance = session.get(Instance, execution.instance_id)
        if not instance.self_service_policy_id:
            return
        try:
            advance(session, instance, publish)
        except Exception as error:
            session.rollback()
            operation = session.get(Operation, execution.operation_id)
            operation.error_code = str(error)[:64] if isinstance(error, Conflict) else "LAB_DISPATCH_UNCONFIRMED"
            event(session, operation, "lab_provision_paused", "worker:iac")
            session.commit()
