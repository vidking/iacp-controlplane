"""Separa la credencial efímera del flujo técnico de bootstrap comercial.

La referencia no es una contraseña: apunta a un secreto efímero en Redis y se
limpia después de verificar el acceso. Los datos comerciales históricos y sus
tablas permanecen intactos.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015_technical_access"
down_revision = "0014_allow_historical_kvm_106"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("instances", sa.Column("technical_access_reference", sa.String(43)))
    op.add_column(
        "instances",
        sa.Column(
            "technical_access_public_keys",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade():
    raise RuntimeError(
        "Downgrade deshabilitado para conservar autorizaciones, el vínculo técnico y la auditoría"
    )
