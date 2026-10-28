"""Adopción transaccional e idempotente del ownership técnico histórico.

La migración solo materializa vínculos cuya evidencia ya está persistida. No
consulta ni modifica Proxmox, Terraform State, credenciales o recursos físicos;
la comprobación física sigue siendo una fase read-only del procedimiento de
promoción.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models import Instance, Operation, TechnicalOwnership, TerraformApply, TerraformExecution
from .instances import Conflict
from .technical_ownership import PROTECTED_VMIDS, record


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
        "reconciliation_required": [],
        "recovery_required": [],
        "protected_excluded": [],
        "failed_without_resource": [],
        "incomplete_without_resource": [],
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
        execution = session.get(TerraformExecution, instance.id)
        active_operations = session.scalars(select(Operation).where(
            Operation.instance_id == instance.id,
            Operation.status.in_({"queued", "running", "uncertain"}),
        )).all()
        active_applies = []
        if execution is not None:
            active_applies = session.scalars(select(TerraformApply).where(
                TerraformApply.instance_id == instance.id,
                TerraformApply.status.in_({"approved", "queued", "running", "uncertain"}),
            )).all()
        if active_operations or active_applies:
            item = {"instance_id": instance.id,
                    "reason": "ACTIVE_OR_UNCERTAIN_OPERATION",
                    "operations": len(active_operations),
                    "applies": len(active_applies)}
            report["recovery_required"].append(item)
            report["blocked"].append(item)
            continue
        protected_vmid = next((vmid for vmid in (instance.vmid, execution.vmid if execution else None)
                               if vmid in PROTECTED_VMIDS), None)
        if protected_vmid is not None:
            report["protected_excluded"].append({
                "instance_id": instance.id,
                "vmid": protected_vmid,
                "reason": "PROTECTED_RESOURCE_EXCLUDED",
            })
            continue
        apply = None
        if execution is not None:
            apply = session.scalar(select(TerraformApply).where(
                TerraformApply.instance_id == instance.id,
                TerraformApply.attempt == execution.attempt,
                TerraformApply.plan_sha256 == execution.plan_sha256,
                TerraformApply.status == "completed",
            ))
        result = apply.result if apply and isinstance(apply.result, dict) else {}
        observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
        physical_evidence = bool(
            instance.vmid is not None or instance.node is not None
            or observation.get("proxmox_present") is True
        )
        if instance.status == "failed" and not physical_evidence:
            report["failed_without_resource"].append({
                "instance_id": instance.id,
                "reason": "FAILED_WITHOUT_VERIFIED_RESOURCE",
            })
            continue
        if instance.status in {"pending", "provisioning"} and not physical_evidence and apply is None:
            report["incomplete_without_resource"].append({
                "instance_id": instance.id,
                "reason": "NO_COMPLETED_APPLY_OR_RESOURCE_EVIDENCE",
            })
            continue
        try:
            with session.begin_nested():
                record(session, instance, adopt=True)
            report["adopted"] += 1
        except Conflict as error:
            item = {"instance_id": instance.id, "reason": str(error)}
            report["reconciliation_required"].append(item)
            report["blocked"].append(item)
        except IntegrityError:
            item = {
                "instance_id": instance.id,
                "reason": "TECHNICAL_OWNERSHIP_COLLISION",
            }
            report["reconciliation_required"].append(item)
            report["blocked"].append(item)
    session.commit()
    report["blocked_count"] = len(report["blocked"])
    return report
