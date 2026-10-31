"""Esquema inicial del orquestador técnico."""

from alembic import op
import sqlalchemy as sa


revision = "0001_technical_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operations",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False, server_default="provision"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text()),
        sa.Column("plan_operation_id", sa.Text()),
        sa.Column("stack", sa.Text(), nullable=False, server_default="mng01"),
        sa.Column("worker_id", sa.Text()),
        sa.Column("vmid", sa.Integer()),
        sa.Column("workspace_path", sa.Text()),
        sa.Column("plan_path", sa.Text()),
        sa.Column("access_ciphertext", sa.Text()),
        sa.Column("access_expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_heartbeat", sa.TIMESTAMP(timezone=True)),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("output", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("idx_operations_status", "operations", ["status", "created_at"])
    op.create_index("idx_operations_instance", "operations", ["instance_id", "created_at"])

    op.create_table(
        "events",
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.Text()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("idx_events_operation", "events", ["operation_id", "event_id"])

    op.create_table(
        "workers",
        sa.Column("worker_id", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("last_heartbeat", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("current_operation_id", sa.Text()),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("heartbeat_count", sa.BigInteger(), nullable=False, server_default="0"),
    )

    op.create_table(
        "instances",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("image", sa.Text(), nullable=False),
        sa.Column("vmid", sa.Integer()),
        sa.Column("node", sa.Text(), nullable=False, server_default="mng01"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("power_state", sa.Text()),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("idx_instances_vmid", "instances", ["vmid"])
    op.create_index(
        "uq_instances_live_name",
        "instances",
        ["name"],
        unique=True,
        postgresql_where=sa.text("status <> 'deleted'"),
    )

    op.create_table(
        "vmid_leases",
        sa.Column("vmid", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("released_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_index("idx_vmid_leases_status", "vmid_leases", ["status", "vmid"])


def downgrade() -> None:
    raise RuntimeError("El esquema técnico no admite downgrade destructivo")
