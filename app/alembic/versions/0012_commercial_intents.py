"""Intenciones comerciales aditivas; no reclasifica ni modifica filas históricas."""
from alembic import op
import sqlalchemy as sa

revision='0012_commercial_intents'
down_revision='0011_crm_contract'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('instances',sa.Column('commercial_revision',sa.String(32)))
    op.create_table('commercial_intents',
        sa.Column('instance_id',sa.String(36),sa.ForeignKey('instances.id'),primary_key=True),
        sa.Column('revision',sa.String(32),nullable=False),
        sa.Column('plan_id',sa.String(32),nullable=False),
        sa.Column('image_id',sa.String(63),nullable=False),
        sa.Column('catalog_sha256',sa.String(64),nullable=False),
        sa.Column('request_fingerprint',sa.String(64),nullable=False),
        sa.Column('specification',sa.JSON(),nullable=False),
        sa.Column('execution_binding',sa.String(64)),
        sa.Column('secret_reference',sa.String(43)),
        sa.Column('bootstrap_status',sa.String(32),nullable=False),
        sa.Column('bootstrap_started_at',sa.DateTime(timezone=True)),
        sa.Column('bootstrap_completed_at',sa.DateTime(timezone=True)),
        sa.Column('error_code',sa.String(64)))


def downgrade():
    raise RuntimeError('Downgrade deshabilitado para conservar autorizaciones, intenciones y auditoría; revertir código compatible')
