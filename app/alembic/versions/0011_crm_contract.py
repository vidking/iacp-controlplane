"""CRM references and optional lifecycle continuation; preserve existing intents."""
from alembic import op
import sqlalchemy as sa

revision = '0011_crm_contract'
down_revision = '0010_guest_identity'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('instances', sa.Column('commercial_context', sa.JSON()))
    op.add_column('instances', sa.Column('start_on_create', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('operations', sa.Column('correlation_id', sa.String(36)))
    op.add_column('instances', sa.Column('initial_access_ciphertext', sa.Text()))
    op.add_column('instances', sa.Column('initial_access_fingerprint', sa.String(64)))
    op.add_column('instances', sa.Column('access_verification', sa.JSON()))
    op.create_index('uq_instances_access_fingerprint', 'instances', ['initial_access_fingerprint'], unique=True)


def downgrade():
    raise RuntimeError('Downgrade deshabilitado para conservar autorizaciones, fences e historial')
