"""Add durable Terraform plans and attempt history; preserve all 2A tables."""
from alembic import op
import sqlalchemy as sa

revision = "0003_terraform_plans"
down_revision = "0002_instances_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "terraform_executions",
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("instances.id"), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), unique=True, nullable=False),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("operations.id"), unique=True, nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("state_key", sa.String(512), unique=True, nullable=False),
        sa.Column("vmid", sa.Integer(), unique=True), sa.Column("allocation", sa.JSON()),
        sa.Column("source_revision", sa.String(40)), sa.Column("plan_sha256", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('queued','running','planned','failed','cancelled')", name="ck_tf_execution_status"),
        sa.CheckConstraint("vmid IS NULL OR vmid <> 104", name="ck_tf_excludes_controlplane"),
    )
    op.create_table(
        "terraform_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("terraform_executions.instance_id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False), sa.Column("retry_key", sa.String(128)),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("error_code", sa.String(64)), sa.Column("plan_sha256", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("instance_id", "number", name="uq_tf_attempt_number"),
        sa.UniqueConstraint("instance_id", "retry_key", name="uq_tf_attempt_retry_key"),
    )


def downgrade():
    raise RuntimeError("Downgrade is disabled to preserve persistent jobs and Terraform plans")
