"""Create jobs or adopt the exact bootstrap schema without modifying its rows."""
from alembic import op
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import sqlalchemy as sa

revision = "0001_bootstrap_jobs"
down_revision = None
branch_labels = None
depends_on = None


def bootstrap_metadata():
    # Frozen definition: never import the evolving application models here.
    metadata = sa.MetaData()
    table = sa.Table(
        "jobs", metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index("ix_jobs_status", table.c.status)
    return metadata


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    metadata = bootstrap_metadata()
    if inspector.has_table("jobs"):
        comparison = MigrationContext.configure(connection, opts={
            "compare_type": True,
            "compare_server_default": True,
            "include_object": lambda obj, name, kind, reflected, compare_to:
                kind != "table" or name == "jobs",
        })
        primary_key = inspector.get_pk_constraint("jobs")["constrained_columns"]
        if (
            primary_key != ["id"]
            or inspector.get_check_constraints("jobs")
            or compare_metadata(comparison, metadata)
        ):
            raise RuntimeError("Existing jobs schema differs from bootstrap; manual review required")
        return

    table = metadata.tables["jobs"]
    op.execute(sa.schema.CreateTable(table))
    op.create_index("ix_jobs_status", "jobs", ["status"], unique=False)


def downgrade():
    raise RuntimeError("Baseline downgrade is disabled to preserve persistent jobs")
