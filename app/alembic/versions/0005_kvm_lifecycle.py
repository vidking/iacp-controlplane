"""Multiple audited operations per instance and durable power task recovery."""
from alembic import op
import sqlalchemy as sa

revision = "0005_kvm_lifecycle"
down_revision = "0004_authorized_apply"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    constraint = next(row for row in sa.inspect(connection).get_unique_constraints("operations")
                      if row["column_names"] == ["instance_id"])
    with op.batch_alter_table("operations", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
        batch.drop_constraint(constraint["name"] or "uq_operations_instance_id", type_="unique")
        batch.drop_constraint("ck_operations_status", type_="check")
        batch.create_check_constraint("ck_operations_status", "status IN ('queued', 'running', 'completed', 'failed', 'uncertain')")
    op.create_index("uq_operations_creation", "operations", ["instance_id"], unique=True,
                    postgresql_where=sa.text("kind = 'instance.create'"), sqlite_where=sa.text("kind = 'instance.create'"))
    op.create_table(
        "power_executions",
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("operations.id"), primary_key=True),
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("vmid", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(128), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("approval_id", sa.String(36), sa.ForeignKey("terraform_applies.id"), nullable=False),
        sa.Column("ownership_revision", sa.String(40), nullable=False),
        sa.Column("task_id", sa.String(512)), sa.Column("result", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("action IN ('start','shutdown','reboot')", name="ck_power_action"),
        sa.CheckConstraint("status IN ('queued','running','uncertain','completed','failed')", name="ck_power_status"),
        sa.CheckConstraint("vmid >= 100 AND vmid <> 104", name="ck_power_excludes_controlplane"),
    )
    op.create_index("uq_power_active_instance", "power_executions", ["instance_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued','running','uncertain')"),
                    sqlite_where=sa.text("status IN ('queued','running','uncertain')"))


def downgrade():
    raise RuntimeError("Downgrade deshabilitado para conservar autorizaciones y operaciones Lifecycle")
