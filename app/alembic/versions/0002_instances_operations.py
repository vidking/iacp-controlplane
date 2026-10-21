"""Add tenant inventory and operation history without rewriting bootstrap jobs."""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0002_instances_operations"
down_revision = "0001_bootstrap_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(63), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    tenants = sa.table("tenants", sa.column("id"), sa.column("slug"), sa.column("name"),
                       sa.column("active", sa.Boolean()), sa.column("created_at", sa.DateTime(timezone=True)))
    op.bulk_insert(tenants, [{
        "id": "00000000-0000-0000-0000-000000000001", "slug": "bootstrap", "name": "Bootstrap",
        "active": True, "created_at": datetime.now(timezone.utc),
    }])
    op.create_table(
        "instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("image", sa.String(63), nullable=False),
        sa.Column("vcpus", sa.Integer(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("disk_gb", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("executor", sa.String(30), nullable=False),
        sa.Column("vmid", sa.Integer()), sa.Column("node", sa.String(128)),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_instances_tenant_name"),
        sa.CheckConstraint("status IN ('pending', 'provisioning', 'ready', 'failed')", name="ck_instances_status"),
    )
    op.create_index("ix_instances_tenant_id", "instances", ["tenant_id"])
    op.create_table(
        "operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("instances.id"), nullable=False, unique=True),
        sa.Column("kind", sa.String(30), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False), sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_operations_tenant_key"),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="ck_operations_status"),
    )
    op.create_index("ix_operations_tenant_id", "operations", ["tenant_id"])
    # PostgreSQL admite ALTER aditivo. SQLite necesita batch para las FK nuevas.
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("operation_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_jobs_tenant", "tenants", ["tenant_id"], ["id"])
        batch.create_foreign_key("fk_jobs_operation", "operations", ["operation_id"], ["id"])
        batch.create_unique_constraint("uq_jobs_operation_id", ["operation_id"])
        batch.create_index("ix_jobs_tenant_id", ["tenant_id"])
    op.create_table(
        "operation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("operations.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("code", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_operation_events_operation_id", "operation_events", ["operation_id"])


def downgrade():
    raise RuntimeError("Downgrade is disabled to preserve persistent jobs and instances")
