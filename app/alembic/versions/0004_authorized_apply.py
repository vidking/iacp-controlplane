"""Agregar autorizaciones de un solo uso sin alterar datos ni migraciones anteriores."""
from alembic import op
import sqlalchemy as sa

revision = "0004_authorized_apply"
down_revision = "0003_terraform_plans"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "terraform_applies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("terraform_executions.instance_id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), unique=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("plan_sha256", sa.String(64), nullable=False),
        sa.Column("approval_key", sa.String(128), nullable=False),
        sa.Column("dispatch_key", sa.String(128)),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("requested_by", sa.String(128)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(64)), sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("instance_id", "attempt", name="uq_tf_apply_plan"),
        sa.UniqueConstraint("instance_id", "approval_key", name="uq_tf_apply_approval_key"),
        sa.CheckConstraint("status IN ('approved','queued','running','completed','failed','uncertain','cancelled')", name="ck_tf_apply_status"),
    )
    op.create_index("ix_terraform_applies_instance_id", "terraform_applies", ["instance_id"])


def downgrade():
    raise RuntimeError("Downgrade deshabilitado para conservar autorizaciones y evidencia de ejecución")
