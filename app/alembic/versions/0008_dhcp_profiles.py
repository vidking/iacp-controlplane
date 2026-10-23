"""Profile policy and network observations, without rewriting historical ownership."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_dhcp_profiles"
down_revision = "0007_safe_deletion"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('instances') as batch:
        batch.add_column(sa.Column('network_profile',sa.String(63)))
        batch.add_column(sa.Column('network_mode',sa.String(16),nullable=False,server_default='static'))
        batch.add_column(sa.Column('ip_addresses',sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'),nullable=False,server_default=sa.text("'[]'")))
        batch.add_column(sa.Column('network_observation',sa.JSON()))
        batch.create_check_constraint('ck_instances_network_mode', "network_mode IN ('static','dhcp')")
    op.create_table('network_discoveries',
        sa.Column('operation_id',sa.String(36),sa.ForeignKey('operations.id'),primary_key=True),
        sa.Column('instance_id',sa.String(36),sa.ForeignKey('instances.id'),nullable=False),
        sa.Column('provisioning_binding',sa.String(64),nullable=False),
        sa.Column('result',sa.JSON()))
    op.create_index('ix_network_discoveries_instance_id','network_discoveries',['instance_id'])


def downgrade():
    raise RuntimeError('Downgrade deshabilitado para conservar autorizaciones y observaciones de red')
