import os
from datetime import datetime, timezone

from api.db import database
from worker.proxmox import ProxmoxClient


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def allocate(operation_id: str, client: ProxmoxClient) -> int:
    minimum = int(os.getenv("IACP_VMID_MIN", "30000"))
    maximum = int(os.getenv("IACP_VMID_MAX", "39999"))
    with database() as db:
        # Un solo lock transaccional coordina los seis workers sin depender de
        # semántica SQLite. El inventario Proxmox se toma bajo ese lock para
        # que dos asignaciones no puedan reservar el mismo candidato.
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("iacp-vmid-allocation",))
        inventory = {
            int(item["vmid"])
            for item in client.inventory()
            if str(item.get("vmid", "")).isdigit()
        }
        reserved = {
            row["vmid"]
            for row in db.execute("SELECT vmid FROM vmid_leases")
        }
        candidate = next((vmid for vmid in range(minimum, maximum + 1) if vmid not in inventory and vmid not in reserved), None)
        if candidate is None:
            raise RuntimeError("VMID_RANGE_EXHAUSTED")
        db.execute(
            """
            INSERT INTO vmid_leases(vmid, operation_id, status, created_at, released_at)
            VALUES (?, ?, 'held', ?, NULL)
            ON CONFLICT(vmid) DO UPDATE SET
              operation_id=excluded.operation_id,
              status='held',
              created_at=excluded.created_at,
              released_at=NULL
            """,
            (candidate, operation_id, now()),
        )
        db.execute("UPDATE operations SET vmid=? WHERE operation_id=?", (candidate, operation_id))
        db.execute(
            "UPDATE instances SET vmid=?, node='mng01', updated_at=? WHERE operation_id=?",
            (candidate, now(), operation_id),
        )
        db.execute("COMMIT")
    return candidate


def release(vmid: int) -> None:
    with database() as db:
        db.execute(
            "UPDATE vmid_leases SET status='released', released_at=? WHERE vmid=?",
            (now(), vmid),
        )
