from datetime import datetime, timezone

from api.db import database


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def heartbeat(
    worker_id: str,
    status: str,
    operation_id: str | None = None,
    *,
    phase: str | None = None,
) -> None:
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

