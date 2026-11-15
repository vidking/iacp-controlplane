import os
from datetime import datetime, timezone

from api.db import database
from api.protection import is_protected_vmid
from worker.proxmox import ProxmoxClient


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def allocate(operation_id: str, client: ProxmoxClient, *, node: str | None = None) -> int:
    minimum = int(os.getenv("IACP_VMID_MIN", "40001"))
    maximum = int(os.getenv("IACP_VMID_MAX", "49999"))
    reuse = os.getenv("IACP_VMID_REUSE", "1").strip().lower() not in {"0", "false", "no"}
    inventory = {int(item["vmid"]) for item in client.inventory() if str(item.get("vmid", "")).isdigit()}
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        reserved = {row["vmid"] for row in db.execute("SELECT vmid FROM vmid_leases WHERE status = 'held'")}
        consumed = (
            {row["vmid"] for row in db.execute("SELECT vmid FROM vmid_leases")}
            if not reuse
            else set()
        )
        candidate = next(
            (
                vmid
                for vmid in range(minimum, maximum + 1)
                if not is_protected_vmid(vmid)
                and vmid not in inventory
                and vmid not in reserved
                and vmid not in consumed
            ),
            None,
        )
        if candidate is None:
            db.execute("ROLLBACK")
            raise RuntimeError("VMID_RANGE_EXHAUSTED")
        # Compare-and-set en la base, además del advisory lock: un lease
        # 'held' nunca se sobrescribe, aunque otro proceso no respete la
        # sección crítica (por ejemplo, un script operativo).
        claimed = db.execute(
            """
            INSERT INTO vmid_leases(vmid, operation_id, status, created_at, released_at)
            VALUES (?, ?, 'held', ?, NULL)
            ON CONFLICT(vmid) DO UPDATE SET
              operation_id=excluded.operation_id,
              status='held',
              created_at=excluded.created_at,
              released_at=NULL
            WHERE vmid_leases.status <> 'held'
            RETURNING vmid
            """,
            (candidate, operation_id, now()),
        ).fetchone()
        if claimed is None:
            db.execute("ROLLBACK")
            raise RuntimeError("VMID_LEASE_CONFLICT")
        db.execute("UPDATE operations SET vmid=? WHERE operation_id=?", (candidate, operation_id))
        db.execute(
            "UPDATE instances SET vmid=?, node=?, updated_at=? WHERE operation_id=?",
            (candidate, node or "mng01", now(), operation_id),
        )
        db.execute("COMMIT")
    return candidate


def release(vmid: int, *, instance_id: str | None = None) -> None:
    """Libera el lease de un VMID.

    Con ``instance_id``, solo se libera si el lease pertenece a una operación
    de esa instancia. Con ``IACP_VMID_REUSE=1`` el mismo VMID puede haber sido
    reasignado a otra instancia, y una operación vieja no debe liberar el
    lease que ya retiene una operación nueva.
    """

    with database() as db:
        if instance_id:
            db.execute(
                """
                UPDATE vmid_leases SET status='released', released_at=?
                WHERE vmid=?
                  AND operation_id IN (SELECT operation_id FROM operations WHERE instance_id=?)
                """,
                (now(), vmid, instance_id),
            )
        else:
            db.execute(
                "UPDATE vmid_leases SET status='released', released_at=? WHERE vmid=?",
                (now(), vmid),
            )
