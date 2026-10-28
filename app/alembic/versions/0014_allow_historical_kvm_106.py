"""Permite administrar la KVM histórica 106 con ownership técnico.

VM 106 es una instancia de laboratorio, no el Control Plane ni una Golden
Image. La migración no crea ni modifica recursos: solo actualiza las
restricciones de las tablas para que el procedimiento idempotente de
``migrate-ownership`` pueda materializar su vínculo técnico verificable.
"""

from alembic import op


revision = "0014_allow_historical_kvm_106"
down_revision = "0013_technical_ownership"
branch_labels = None
depends_on = None

_CHECK = "(vmid = 106 OR vmid >= 30000) AND vmid NOT IN (104,29000,29001,29010,29011)"


def upgrade():
    with op.batch_alter_table("technical_ownership") as batch:
        batch.drop_constraint("ck_technical_ownership_disposable_vmid", type_="check")
        batch.create_check_constraint("ck_technical_ownership_disposable_vmid", _CHECK)
    with op.batch_alter_table("deletion_executions") as batch:
        batch.drop_constraint("ck_deletion_disposable_vmid", type_="check")
        batch.create_check_constraint("ck_deletion_disposable_vmid", _CHECK)


def downgrade():
    raise RuntimeError("Downgrade deshabilitado: conservar autorizaciones, ownership técnico y auditoría")
