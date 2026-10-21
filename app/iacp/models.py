from datetime import datetime, timezone
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


BOOTSTRAP_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Instance(Base):
    __tablename__ = "instances"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_instances_tenant_name"),
        CheckConstraint("status IN ('pending', 'provisioning', 'ready', 'failed')", name="ck_instances_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    image: Mapped[str] = mapped_column(String(63), nullable=False)
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
    creation: Mapped["Operation"] = relationship(uselist=False)

    @property
    def operation_id(self) -> str:
        return self.creation.id

    @property
    def job_id(self) -> str:
        return self.creation.job.id


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_operations_tenant_key"),
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="ck_operations_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
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
