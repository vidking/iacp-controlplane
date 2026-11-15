import os
import time
from datetime import datetime, timezone

from api.db import database


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


_last_sent: dict[str, tuple[tuple, float]] = {}


def _min_interval() -> float:
    try:
        return max(0.0, float(os.getenv("IACP_HEARTBEAT_MIN_INTERVAL_SECONDS", "5")))
    except ValueError:
        return 5.0


def heartbeat(
    worker_id: str,
    status: str,
    operation_id: str | None = None,
    *,
    phase: str | None = None,
) -> None:
    # Los callbacks de Proxmox, Guest Agent y reconciliación llaman a
    # heartbeat() en bucles de ~1 s. Un heartbeat idéntico (mismo worker,
    # estado, operación y fase) dentro del intervalo mínimo no aporta nada
    # frente al umbral de stale (900 s). Cualquier cambio de estado, operación
    # o fase se escribe siempre de inmediato.
    key = (status, operation_id, phase)
    previous = _last_sent.get(worker_id)
    current = time.monotonic()
    if previous is not None and previous[0] == key and current - previous[1] < _min_interval():
        return
    timestamp = now()
    with database() as db:
        db.execute(
            """
            INSERT INTO workers(
              worker_id, status, last_heartbeat, current_operation_id, started_at, heartbeat_count
            )
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(worker_id) DO UPDATE SET
              status = excluded.status,
              last_heartbeat = excluded.last_heartbeat,
              current_operation_id = excluded.current_operation_id,
              started_at = CASE
                WHEN excluded.status='starting' THEN excluded.started_at
                ELSE COALESCE(workers.started_at, excluded.started_at)
              END,
              heartbeat_count = workers.heartbeat_count + 1
            """,
            (worker_id, status, timestamp, operation_id, timestamp),
        )
        if operation_id:
            if phase:
                db.execute(
                    """
                    UPDATE operations
                    SET last_heartbeat=?, phase=?
                    WHERE operation_id=?
                      AND status IN ('running', 'planning', 'applying', 'destroying', 'powering')
                    """,
                    (timestamp, phase, operation_id),
                )
            else:
                db.execute(
                    """
                    UPDATE operations
                    SET last_heartbeat=?
                    WHERE operation_id=?
                      AND status IN ('running', 'planning', 'applying', 'destroying', 'powering')
                    """,
                    (timestamp, operation_id),
                )
    _last_sent[worker_id] = (key, current)

