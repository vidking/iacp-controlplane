import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone


def db_path() -> str:
    return os.getenv("IACP_DB_PATH", "data/iacp.sqlite")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def database():
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(db: sqlite3.Connection) -> None:
    operation_columns = _columns(db, "operations")
    operation_additions = {
        "instance_id": "TEXT",
        "action": "TEXT NOT NULL DEFAULT 'provision'",
        "plan_id": "TEXT",
        "vmid": "INTEGER",
        "workspace_path": "TEXT",
        "plan_path": "TEXT",
        "plan_operation_id": "TEXT",
        "stack": "TEXT NOT NULL DEFAULT 'mng01'",
        "target_node": "TEXT",
        "secret_path": "TEXT",
        "commercial_context_json": "TEXT NOT NULL DEFAULT '{}'",
        "phase": "TEXT NOT NULL DEFAULT 'queued'",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in operation_additions.items():
        if name not in operation_columns:
            db.execute(f"ALTER TABLE operations ADD COLUMN {name} {definition}")

    instance_columns = _columns(db, "instances")
    instance_additions = {
        "plan_id": "TEXT",
        "commercial_context_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    for name, definition in instance_additions.items():
        if name not in instance_columns:
            db.execute(f"ALTER TABLE instances ADD COLUMN {name} {definition}")

    worker_columns = _columns(db, "workers")
    worker_additions = {
        "started_at": "TEXT",
        "heartbeat_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in worker_additions.items():
        if name not in worker_columns:
            db.execute(f"ALTER TABLE workers ADD COLUMN {name} {definition}")


def initialize() -> None:
    directory = os.path.dirname(db_path())
    if directory:
        os.makedirs(directory, exist_ok=True)
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                plan_id TEXT,
                commercial_context_json TEXT NOT NULL DEFAULT '{}',
                secret_path TEXT,
                target_node TEXT,
                worker_id TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                last_heartbeat TEXT,
                phase TEXT NOT NULL DEFAULT 'queued',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                exit_code INTEGER,
                output TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_operations_heartbeat ON operations(status, last_heartbeat);
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_events_operation_id ON events(operation_id, event_id);
            CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id);
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                current_operation_id TEXT,
                started_at TEXT,
                heartbeat_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_workers_heartbeat ON workers(last_heartbeat);
            CREATE TABLE IF NOT EXISTS instances (
                instance_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                name TEXT NOT NULL,
                image TEXT NOT NULL,
                plan_id TEXT,
                vmid INTEGER,
                node TEXT NOT NULL DEFAULT 'mng01',
                status TEXT NOT NULL,
                power_state TEXT,
                spec_json TEXT NOT NULL,
                commercial_context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_instances_vmid ON instances(vmid);
            CREATE INDEX IF NOT EXISTS idx_instances_status_power ON instances(status, power_state, updated_at);
            CREATE TABLE IF NOT EXISTS vmid_leases (
                vmid INTEGER PRIMARY KEY,
                operation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                released_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ip_allocations (
                allocation_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'netbox',
                profile TEXT NOT NULL,
                prefix_id INTEGER NOT NULL,
                netbox_ip_id INTEGER,
                address TEXT NOT NULL DEFAULT '',
                gateway TEXT NOT NULL DEFAULT '',
                marker TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                released_at TEXT,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ip_allocations_active_instance
              ON ip_allocations(instance_id, profile)
              WHERE status IN ('allocating', 'allocated', 'release_pending');
            CREATE INDEX IF NOT EXISTS idx_ip_allocations_operation
              ON ip_allocations(operation_id, status);
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                operation_id TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (scope, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_idempotency_created_at ON idempotency_keys(created_at);
            """
        )
        _add_missing_columns(db)
        db.execute("CREATE INDEX IF NOT EXISTS idx_instances_plan_id ON instances(plan_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_operations_heartbeat ON operations(status, last_heartbeat)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_operations_instance_status ON operations(instance_id, status, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_instances_status_power ON instances(status, power_state, updated_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_workers_heartbeat ON workers(last_heartbeat)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_idempotency_created_at ON idempotency_keys(created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_ip_allocations_operation ON ip_allocations(operation_id, status)")
        if "power_state" not in _columns(db, "instances"):
            db.execute("ALTER TABLE instances ADD COLUMN power_state TEXT")


def prune_history() -> dict[str, int]:
    """Optionally compact terminal history without touching active work.

    Retention is opt-in so a local developer or an auditor never loses data
    merely by starting the API. Production can set IACP_RETENTION_DAYS after
    arranging the desired backup policy.
    """

    days = int(os.getenv("IACP_RETENTION_DAYS", "0"))
    if days <= 0:
        return {"events": 0, "operations": 0, "idempotency_keys": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        event_result = db.execute(
            """
            DELETE FROM events
            WHERE occurred_at < ?
              AND operation_id IN (
                SELECT operation_id FROM operations
                WHERE status IN ('succeeded', 'failed', 'uncertain')
                  AND finished_at IS NOT NULL
                  AND finished_at < ?
              )
            """,
            (cutoff, cutoff),
        )
        idempotency_result = db.execute(
            """
            DELETE FROM idempotency_keys
            WHERE created_at < ?
              AND (
                operation_id IS NULL
                OR operation_id IN (
                  SELECT operation_id FROM operations
                  WHERE status IN ('succeeded', 'failed', 'uncertain')
                    AND finished_at IS NOT NULL
                    AND finished_at < ?
                )
              )
            """,
            (cutoff, cutoff),
        )
        operation_result = db.execute(
            """
            DELETE FROM operations
            WHERE status IN ('succeeded', 'failed', 'uncertain')
              AND finished_at IS NOT NULL
              AND finished_at < ?
            """,
            (cutoff,),
        )
        db.execute("COMMIT")
    return {
        "events": event_result.rowcount,
        "operations": operation_result.rowcount,
        "idempotency_keys": idempotency_result.rowcount,
    }

