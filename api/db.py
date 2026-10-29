import os
import sqlite3
from contextlib import contextmanager


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
    existing = _columns(db, "operations")
    additions = {
        "instance_id": "TEXT",
        "action": "TEXT NOT NULL DEFAULT 'provision'",
        "vmid": "INTEGER",
        "workspace_path": "TEXT",
        "plan_path": "TEXT",
        "plan_operation_id": "TEXT",
        "stack": "TEXT NOT NULL DEFAULT 'mng01'",
    }
    for name, definition in additions.items():
        if name not in existing:
            db.execute(f"ALTER TABLE operations ADD COLUMN {name} {definition}")


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
                worker_id TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                last_heartbeat TEXT,
                exit_code INTEGER,
                output TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status, created_at);
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                current_operation_id TEXT
            );
            CREATE TABLE IF NOT EXISTS instances (
                instance_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                name TEXT NOT NULL,
                image TEXT NOT NULL,
                vmid INTEGER,
                node TEXT NOT NULL DEFAULT 'mng01',
                status TEXT NOT NULL,
                power_state TEXT,
                spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_instances_vmid ON instances(vmid);
            CREATE TABLE IF NOT EXISTS vmid_leases (
                vmid INTEGER PRIMARY KEY,
                operation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                released_at TEXT
            );
            """
        )
        _add_missing_columns(db)
        if "power_state" not in _columns(db, "instances"):
            db.execute("ALTER TABLE instances ADD COLUMN power_state TEXT")

