"""Detección de drift entre el inventario de IACP y Proxmox VE (solo lectura).

El reporte nunca corrige ni elimina nada: clasifica las diferencias para que
un operador las revise y, cuando corresponda, use la reconciliación
existente (``POST /v1/instances/{id}/reconcile``). Una VM huérfana o con
identidad distinta puede ser trabajo manual legítimo en el clúster MNG, por
lo que la acción automática segura es no tocarla y no reutilizar su VMID
(``worker.allocation.allocate`` ya excluye todo VMID presente en Proxmox).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from api.db import database
from api.protection import is_protected_vmid


ACTIVE_STATUSES = ("pending", "running", "planning", "applying", "destroying", "powering")


def managed_range() -> tuple[int, int]:
    return (
        int(os.getenv("IACP_VMID_MIN", "40001")),
        int(os.getenv("IACP_VMID_MAX", "49999")),
    )


def drift_report(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    """Compara el inventario de Proxmox con instancias y leases de IACP."""

    minimum, maximum = managed_range()
    physical: dict[int, dict[str, Any]] = {}
    for item in inventory:
        try:
            vmid = int(item.get("vmid"))
        except (TypeError, ValueError):
            continue
        physical[vmid] = item

    with database() as db:
        instances = db.execute(
            """
            SELECT i.instance_id, i.name, i.vmid, i.node, i.status,
                   EXISTS (
                     SELECT 1 FROM operations o
                     WHERE o.instance_id = i.instance_id AND o.status IN ({active})
                   ) AS busy
            FROM instances i
            WHERE i.vmid IS NOT NULL AND i.status <> 'deleted'
            ORDER BY i.vmid
            """.format(active=",".join("?" for _ in ACTIVE_STATUSES)),
            ACTIVE_STATUSES,
        ).fetchall()
        held_leases = db.execute(
            """
            SELECT l.vmid, l.operation_id, l.created_at, o.status AS operation_status
            FROM vmid_leases l
            LEFT JOIN operations o ON o.operation_id = l.operation_id
            WHERE l.status = 'held'
            ORDER BY l.vmid
            """
        ).fetchall()

    known_vmids = {int(row["vmid"]) for row in instances}
    missing_in_proxmox = []
    identity_mismatch = []
    for row in instances:
        if row["busy"]:
            # Una operación en curso puede estar creando o destruyendo la VM.
            continue
        observed = physical.get(int(row["vmid"]))
        if observed is None:
            if row["status"] not in {"failed"}:
                missing_in_proxmox.append(
                    {"instance_id": row["instance_id"], "vmid": int(row["vmid"]), "status": row["status"]}
                )
            continue
        if observed.get("name") != row["name"] or (row["node"] and observed.get("node") != row["node"]):
            identity_mismatch.append(
                {
                    "instance_id": row["instance_id"],
                    "vmid": int(row["vmid"]),
                    "expected": {"name": row["name"], "node": row["node"]},
                    "observed": {"name": observed.get("name"), "node": observed.get("node")},
                }
            )

    lease_vmids = {int(row["vmid"]) for row in held_leases}
    orphans = [
        {"vmid": vmid, "name": item.get("name"), "node": item.get("node")}
        for vmid, item in sorted(physical.items())
        if minimum <= vmid <= maximum
        and vmid not in known_vmids
        and vmid not in lease_vmids
        and not is_protected_vmid(vmid)
        and not item.get("template")
    ]
    stale_leases = [
        {"vmid": int(row["vmid"]), "operation_id": row["operation_id"], "operation_status": row["operation_status"]}
        for row in held_leases
        if row["operation_status"] in {None, "failed"} and int(row["vmid"]) not in physical
    ]
    findings = len(orphans) + len(missing_in_proxmox) + len(identity_mismatch) + len(stale_leases)
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "vmid_range": {"min": minimum, "max": maximum},
        "status": "drift" if findings else "consistent",
        "orphaned_in_proxmox": orphans,
        "missing_in_proxmox": missing_in_proxmox,
        "identity_mismatch": identity_mismatch,
        "stale_vmid_leases": stale_leases,
        "automatic_actions": "none",
    }
