"""One-shot guest identity initialization for newly delegated lab clones only."""
from alembic import op
import sqlalchemy as sa

revision = '0010_guest_identity'
down_revision = '0009_lab_self_service'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('guest_initializations',
        sa.Column('operation_id', sa.String(36), sa.ForeignKey('operations.id'), primary_key=True),
        sa.Column('instance_id', sa.String(36), sa.ForeignKey('instances.id'), nullable=False, unique=True),
        sa.Column('provisioning_binding', sa.String(64), nullable=False),
        sa.Column('mode', sa.String(64), nullable=False),
        sa.Column('program_sha256', sa.String(64), nullable=False),
        sa.Column('mac_address', sa.String(17), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True)),
        sa.Column('pid', sa.Integer()),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.Column('result', sa.JSON()),
        sa.CheckConstraint("status IN ('queued','running','uncertain','completed','failed')", name='ck_guest_initialization_status'))


def downgrade():
    raise RuntimeError('Downgrade deshabilitado para conservar autorizaciones, fences e historial')
