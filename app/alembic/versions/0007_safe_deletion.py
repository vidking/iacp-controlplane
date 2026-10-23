"""Disposable KVM deletion, retaining provisioning records and tombstones."""
from alembic import op
import sqlalchemy as sa

revision = "0007_safe_deletion"
down_revision = "0006_power_ownership"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("instances") as batch:
        batch.drop_constraint("ck_instances_status", type_="check")
        batch.create_check_constraint("ck_instances_status", "status IN ('pending','provisioning','ready','failed','deleting','deleted')")
    op.create_table(
        "deletion_executions",
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("operations.id"), primary_key=True),
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("vmid", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(128), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("provisioning_binding", sa.String(64), nullable=False),
        sa.Column("plan_sha256", sa.String(64)),
        sa.Column("approval_key", sa.String(128)),
        sa.Column("approval_hash", sa.String(64)),
        sa.Column("approved_by", sa.String(128)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result", sa.JSON()),
        sa.CheckConstraint("status IN ('queued','running','planned','approved','uncertain','completed','failed')", name="ck_deletion_status"),
        sa.CheckConstraint("vmid >= 30000 AND vmid NOT IN (104,106)", name="ck_deletion_disposable_vmid"),
    )
    op.create_index("uq_deletion_active_instance", "deletion_executions", ["instance_id"], unique=True,
                    postgresql_where=sa.text("status <> 'failed'"), sqlite_where=sa.text("status <> 'failed'"))


def downgrade():
    raise RuntimeError("Downgrade deshabilitado para conservar autorizaciones y tombstones de eliminación")
