"""Adopción transaccional e idempotente del ownership técnico histórico.

La migración solo materializa vínculos cuya evidencia ya está persistida. No
consulta ni modifica Proxmox, Terraform State, credenciales o recursos físicos;
la comprobación física sigue siendo una fase read-only del procedimiento de
promoción.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models import Instance, TechnicalOwnership
from .instances import Conflict
from .technical_ownership import record


def migrate(session):
    """Materializa filas faltantes y devuelve un reporte sanitizado.

    Cada fila usa un savepoint para que una evidencia insuficiente o una
    colisión no aborte la adopción de las demás. El commit del lote lo hace el
    llamador, de modo que la operación es atómica para el conjunto de cambios
    exitosos y reejecutable sin duplicados.
    """

    report = {
        "scanned": 0,
        "adopted": 0,
        "already_bound": 0,
        "deleted": 0,
        "blocked": [],
    }
    rows = session.scalars(select(Instance).order_by(Instance.created_at, Instance.id)).all()
    for instance in rows:
        report["scanned"] += 1
        if instance.status == "deleted":
            report["deleted"] += 1
            continue
        if session.get(TechnicalOwnership, instance.id) is not None:
            report["already_bound"] += 1
            continue
        try:
            with session.begin_nested():
                record(session, instance, adopt=True)
            report["adopted"] += 1
        except Conflict as error:
            report["blocked"].append({"instance_id": instance.id, "reason": str(error)})
        except IntegrityError:
            report["blocked"].append({
                "instance_id": instance.id,
                "reason": "TECHNICAL_OWNERSHIP_COLLISION",
            })
    session.commit()
    report["blocked_count"] = len(report["blocked"])
    return report
