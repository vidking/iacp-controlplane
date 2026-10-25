from datetime import datetime, timezone
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB
from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def operation_correlation(context):
    from .services.tracing import correlation_id
    return correlation_id.get() or context.get_current_parameters()['id']


BOOTSTRAP_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LabPolicy(Base):
    __tablename__ = "lab_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_lab_policy_tenant_key"),
        CheckConstraint("status IN ('proposed','approved','revoked')", name="ck_lab_policy_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    specification: Mapped[dict] = mapped_column(JSON, nullable=False)
    profile_snapshots: Mapped[dict] = mapped_column(JSON, nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    review_history: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), default=list, server_default=text("'[]'"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(128))


class Instance(Base):
    __tablename__ = "instances"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_instances_tenant_name"),
        Index('uq_instances_access_fingerprint', 'initial_access_fingerprint', unique=True),
        CheckConstraint("network_mode IN ('static','dhcp')", name="ck_instances_network_mode"),
        CheckConstraint("status IN ('pending', 'provisioning', 'ready', 'failed', 'deleting', 'deleted')", name="ck_instances_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    image: Mapped[str] = mapped_column(String(63), nullable=False)
    network_profile: Mapped[str | None] = mapped_column(String(63))
    node_preference: Mapped[str | None] = mapped_column(String(128))
    self_service_policy_id: Mapped[str | None] = mapped_column(ForeignKey("lab_policies.id"))
    policy_binding_sha256: Mapped[str | None] = mapped_column(String(64))
    commercial_context: Mapped[dict | None] = mapped_column(JSON)
    start_on_create: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    initial_access_ciphertext: Mapped[str | None] = mapped_column(Text)
    initial_access_fingerprint: Mapped[str | None] = mapped_column(String(64))
    commercial_revision: Mapped[str | None] = mapped_column(String(32))
    access_verification: Mapped[dict | None] = mapped_column(JSON)
    network_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="static", server_default="static")
    ip_addresses: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list, server_default=text("'[]'"))
    network_observation: Mapped[dict | None] = mapped_column(JSON)
    vcpus: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    executor: Mapped[str] = mapped_column(String(30), default="simulated", nullable=False)
    vmid: Mapped[int | None] = mapped_column(Integer)
    node: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    creation: Mapped["Operation"] = relationship(
        primaryjoin="and_(Instance.id == Operation.instance_id, Operation.kind == 'instance.create')",
        uselist=False, viewonly=True)

    @property
    def access(self):
        return ({'username': 'root', **(self.access_verification or {'status': 'pending'})}
            if self.initial_access_ciphertext or self.commercial_revision else None)

    @property
    def infrastructure_kind(self) -> str:
        if self.executor == "simulated":
            return "simulated"
        return "kvm" if self.vmid is not None and self.node is not None else "unprovisioned"

    @property
    def operation_id(self) -> str:
        return self.creation.id

    @property
    def job_id(self) -> str:
        return self.creation.job.id


class CommercialIntent(Base):
    """Metadatos G: nunca contraseña ni ciphertext; referencias efímeras opacas."""
    __tablename__ = 'commercial_intents'
    instance_id: Mapped[str] = mapped_column(ForeignKey('instances.id'), primary_key=True)
    revision: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(32), nullable=False)
    image_id: Mapped[str] = mapped_column(String(63), nullable=False)
    catalog_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    specification: Mapped[dict] = mapped_column(JSON, nullable=False)
    execution_binding: Mapped[str | None] = mapped_column(String(64))
    secret_reference: Mapped[str | None] = mapped_column(String(43))
    bootstrap_status: Mapped[str] = mapped_column(String(32), nullable=False, default='pending')
    bootstrap_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bootstrap_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_operations_tenant_key"),
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'uncertain')", name="ck_operations_status"),
        Index("uq_operations_creation", "instance_id", unique=True,
              postgresql_where=text("kind = 'instance.create'"), sqlite_where=text("kind = 'instance.create'")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(36), default=operation_correlation)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    job: Mapped["Job"] = relationship(uselist=False)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    # NULL conserva los jobs históricos; solo el tenant bootstrap puede leerlos.
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"), unique=True)


class OperationEvent(Base):
    __tablename__ = "operation_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TerraformExecution(Base):
    __tablename__ = "terraform_executions"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','planned','failed','cancelled')", name="ck_tf_execution_status"),
        CheckConstraint("vmid IS NULL OR vmid <> 104", name="ck_tf_excludes_controlplane"),
    )
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True, nullable=False)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    state_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    vmid: Mapped[int | None] = mapped_column(Integer, unique=True)
    allocation: Mapped[dict | None] = mapped_column(JSON)
    source_revision: Mapped[str | None] = mapped_column(String(40))
    plan_sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TerraformAttempt(Base):
    __tablename__ = "terraform_attempts"
    __table_args__ = (
        UniqueConstraint("instance_id", "number", name="uq_tf_attempt_number"),
        UniqueConstraint("instance_id", "retry_key", name="uq_tf_attempt_retry_key"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("terraform_executions.instance_id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_key: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    plan_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TerraformApply(Base):
    __tablename__ = "terraform_applies"
    __table_args__ = (
        UniqueConstraint("instance_id", "attempt", name="uq_tf_apply_plan"),
        UniqueConstraint("instance_id", "approval_key", name="uq_tf_apply_approval_key"),
        CheckConstraint("status IN ('approved','queued','running','completed','failed','uncertain','cancelled')", name="ck_tf_apply_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("terraform_executions.instance_id"), nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), unique=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_key: Mapped[str] = mapped_column(String(128), nullable=False)
    dispatch_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PowerOwnershipCertificate(Base):
    __tablename__ = "power_ownership_certificates"
    __table_args__ = (
        UniqueConstraint("approval_id", "ownership_revision", name="uq_power_certificate_approval_revision"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), nullable=False, index=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("terraform_applies.id"), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    ownership_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    certified_by: Mapped[str] = mapped_column(String(128), nullable=False)
    change_reference: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PowerExecution(Base):
    __tablename__ = "power_executions"
    __table_args__ = (
        CheckConstraint("action IN ('start','shutdown','reboot')", name="ck_power_action"),
        CheckConstraint("status IN ('queued','running','uncertain','completed','failed')", name="ck_power_status"),
        CheckConstraint("vmid >= 100 AND vmid <> 104", name="ck_power_excludes_controlplane"),
        Index("uq_power_active_instance", "instance_id", unique=True,
              postgresql_where=text("status IN ('queued','running','uncertain')"),
              sqlite_where=text("status IN ('queued','running','uncertain')")),
    )
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    phase: Mapped[str] = mapped_column(String(30), nullable=False)
    vmid: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    approval_id: Mapped[str] = mapped_column(ForeignKey("terraform_applies.id"), nullable=False)
    ownership_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    ownership_certificate_id: Mapped[str | None] = mapped_column(ForeignKey("power_ownership_certificates.id"))
    task_id: Mapped[str | None] = mapped_column(String(512))
    result: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeletionExecution(Base):
    __tablename__ = "deletion_executions"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','planned','approved','uncertain','completed','failed')", name="ck_deletion_status"),
        CheckConstraint("vmid >= 30000 AND vmid NOT IN (104,106)", name="ck_deletion_disposable_vmid"),
        Index("uq_deletion_active_instance", "instance_id", unique=True,
              postgresql_where=text("status <> 'failed'"), sqlite_where=text("status <> 'failed'")),
    )
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    autonomous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    phase: Mapped[str] = mapped_column(String(30), nullable=False)
    vmid: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    provisioning_binding: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_sha256: Mapped[str | None] = mapped_column(String(64))
    approval_key: Mapped[str | None] = mapped_column(String(128))
    approval_hash: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)


class NetworkDiscovery(Base):
    __tablename__ = "network_discoveries"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), nullable=False, index=True)
    provisioning_binding: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON)


class GuestInitialization(Base):
    __tablename__ = 'guest_initializations'
    __table_args__ = (CheckConstraint("status IN ('queued','running','uncertain','completed','failed')", name='ck_guest_initialization_status'),)
    operation_id: Mapped[str] = mapped_column(ForeignKey('operations.id'), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey('instances.id'), nullable=False, unique=True)
    provisioning_binding: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    program_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mac_address: Mapped[str] = mapped_column(String(17), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pid: Mapped[int | None] = mapped_column(Integer)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
