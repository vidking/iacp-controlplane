from datetime import datetime, timezone

from api.db import database


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def heartbeat(worker_id: str, status: str, operation_id: str | None = None) -> None:
    timestamp = now()
    with database() as db:
        db.execute(
            """
            INSERT INTO workers(worker_id, status, last_heartbeat, current_operation_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
              status = excluded.status,
              last_heartbeat = excluded.last_heartbeat,
              current_operation_id = excluded.current_operation_id,
              heartbeat_count = workers.heartbeat_count + 1
            """,
            (worker_id, status, timestamp, operation_id),
        )
