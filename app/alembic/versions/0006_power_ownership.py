"""Explicit historical ownership certification, without backfilling any instance."""
from alembic import op
import sqlalchemy as sa

revision = "0006_power_ownership"
down_revision = "0005_kvm_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "power_ownership_certificates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("approval_id", sa.String(36), sa.ForeignKey("terraform_applies.id"), nullable=False),
        sa.Column("source_revision", sa.String(40), nullable=False),
        sa.Column("ownership_revision", sa.String(40), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("certified_by", sa.String(128), nullable=False),
        sa.Column("change_reference", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("approval_id", "ownership_revision", name="uq_power_certificate_approval_revision"),
    )
    op.create_index("ix_power_ownership_certificates_instance_id", "power_ownership_certificates", ["instance_id"])
    with op.batch_alter_table("power_executions") as batch:
        batch.add_column(sa.Column("ownership_certificate_id", sa.String(36)))
        batch.create_foreign_key("fk_power_ownership_certificate", "power_ownership_certificates", ["ownership_certificate_id"], ["id"])


def downgrade():
    raise RuntimeError("Downgrade deshabilitado para conservar autorizaciones y certificados Lifecycle")
