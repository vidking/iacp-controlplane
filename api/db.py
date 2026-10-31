"""PostgreSQL persistence for the technical control plane.

The V2 API/worker contract intentionally remains small, but its durable queue,
heartbeats and leases live in PostgreSQL rather than a local SQLite file.
"""

from contextlib import contextmanager
import os
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row


def _read_secret(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def database_url() -> str:
    configured = os.getenv("IACP_DATABASE_URL", "").strip()
    if configured:
        return configured
    host = os.getenv("DATABASE_HOST", "db")
    port = os.getenv("DATABASE_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "iacp")
    database = os.getenv("POSTGRES_DB", "iacp_technical")
    password_file = os.getenv("DATABASE_PASSWORD_FILE", "/run/secrets/db_password")
    password = _read_secret(password_file) if os.path.exists(password_file) else os.getenv("POSTGRES_PASSWORD", "")
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


class Database:
    """Small compatibility wrapper using psycopg named rows and %s binds."""

    def __init__(self, connection: psycopg.Connection):
        self.connection = connection

    @staticmethod
    def _bind(statement: str) -> str:
        return statement.replace("?", "%s")

    def execute(self, statement: str, params=()):
        return self.connection.execute(self._bind(statement), params)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()


def connect() -> psycopg.Connection:
    return psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=10)


@contextmanager
def database():
    connection = connect()
    db = Database(connection)
    try:
        yield db
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize() -> None:
    with database() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'provision',
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                instance_id TEXT,
                plan_operation_id TEXT,
                stack TEXT NOT NULL DEFAULT 'mng01',
                worker_id TEXT,
                vmid INTEGER,
                workspace_path TEXT,
                plan_path TEXT,
                access_ciphertext TEXT,
                access_expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL,
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                last_heartbeat TIMESTAMPTZ,
                exit_code INTEGER,
                output TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_operations_instance ON operations(instance_id, created_at)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id BIGSERIAL PRIMARY KEY,
                operation_id TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_events_operation ON events(operation_id, event_id)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_heartbeat TIMESTAMPTZ NOT NULL,
                current_operation_id TEXT,
                started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                heartbeat_count BIGINT NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            """
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
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_instances_vmid ON instances(vmid)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_instances_live_name ON instances(name) WHERE status <> 'deleted'")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS vmid_leases (
                vmid INTEGER PRIMARY KEY,
                operation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                released_at TIMESTAMPTZ
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_vmid_leases_status ON vmid_leases(status, vmid)")


def schema_revision() -> str | None:
    """Devuelve la revisión Alembic aplicada sin exponer errores de conexión."""
    try:
        with database() as db:
            row = db.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num LIMIT 1"
            ).fetchone()
        return row["version_num"] if row else None
    except Exception:
        return None
