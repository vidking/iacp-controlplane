"""Bounded, independently approved lab policies; historical instances remain unbound."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_lab_self_service"
down_revision = "0008_dhcp_profiles"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("lab_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("approved_by", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("specification", sa.JSON(), nullable=False),
        sa.Column("profile_snapshots", sa.JSON(), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("review_history", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.String(128)),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_lab_policy_tenant_key"),
        sa.CheckConstraint("status IN ('proposed','approved','revoked')", name="ck_lab_policy_status"))
    op.create_index("ix_lab_policies_tenant_id", "lab_policies", ["tenant_id"])
    with op.batch_alter_table("instances") as batch:
        batch.add_column(sa.Column("node_preference", sa.String(128)))
        batch.add_column(sa.Column("self_service_policy_id", sa.String(36)))
        batch.add_column(sa.Column("policy_binding_sha256", sa.String(64)))
        batch.create_foreign_key("fk_instances_lab_policy", "lab_policies", ["self_service_policy_id"], ["id"])
    with op.batch_alter_table("deletion_executions") as batch:
        batch.add_column(sa.Column("autonomous", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade():
    raise RuntimeError("Downgrade deshabilitado para conservar autorizaciones y propiedad")
