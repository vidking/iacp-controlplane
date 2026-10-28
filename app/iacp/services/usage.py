"""Contadores técnicos de reservas y operaciones por tenant.

El cálculo se conserva fuera de ``lab.py`` porque lo usan el catálogo y el
contrato Core. No representa cuotas comerciales ni una política de aprobación.
"""

from sqlalchemy import select

from ..models import Instance, Operation, TerraformApply, TerraformExecution


def usage(session, tenant_id, exclude_operation=None):
    """Cuenta reservas técnicas hasta que una eliminación queda verificada."""
    rows = session.scalars(
        select(Instance).where(Instance.tenant_id == tenant_id, Instance.status != "deleted")
    ).all()
    active = session.scalars(
        select(Operation).where(
            Operation.tenant_id == tenant_id,
            Operation.status.in_(["queued", "running", "uncertain"]),
            Operation.kind != "instance.network.discover",
        )
    ).all()
    active_ids = {row.id for row in active}
    active_ids.update(
        session.scalars(
            select(TerraformExecution.operation_id)
            .join(Instance)
            .join(TerraformApply, TerraformApply.instance_id == Instance.id)
            .where(
                Instance.tenant_id == tenant_id,
                TerraformApply.status.in_(["approved", "queued", "running", "uncertain"]),
            )
        )
    )
    return {
        "instances": len(rows),
        "vcpus": sum(row.vcpus for row in rows),
        "memory_mb": sum(row.memory_mb for row in rows),
        "disk_gb": sum(row.disk_gb for row in rows),
        "concurrent_operations": len(active_ids - {exclude_operation}),
    }
