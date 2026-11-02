"""Persistencia PostgreSQL compartida por la API y los workers.

La aplicación comienza con una base vacía. Este módulo no importa registros
históricos de SQLite ni datos del runtime anterior.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _connection_options() -> str | dict[str, Any]:
    conninfo = os.getenv("IACP_DATABASE_URL", "").strip()
    if conninfo:
        return conninfo

    password = os.getenv("PGPASSWORD", "")
    if not password:
        raise RuntimeError("PGPASSWORD o IACP_DATABASE_URL debe estar configurado")
    return {
        "host": os.getenv("PGHOST", "postgres"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "iacp_controlplane"),
        "user": os.getenv("PGUSER", "iacp"),
        "password": password,
        "connect_timeout": max(1, int(os.getenv("IACP_DB_CONNECT_TIMEOUT", "5"))),
    }


def _postgres_sql(statement: str) -> str:
    """Translate the repository's qmark parameters to psycopg parameters."""

    if statement.strip().upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    output: list[str] = []
    quote: str | None = None
    escaped = False
    for character in statement:
        if quote:
            output.append(character)
            if character == quote and not escaped:
                quote = None
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
            continue
        if character in {"'", '"'}:
            quote = character
            output.append(character)
        elif character == "?":
            output.append("%s")
        else:
            output.append(character)
    return "".join(output)


class DatabaseConnection:
    """Adaptador pequeño para consultas parametrizadas existentes."""

    def __init__(self, connection: psycopg.Connection):
        self._connection = connection

    def execute(self, statement: str, parameters: Any = ()):
        return self._connection.execute(_postgres_sql(statement), parameters)

    def executemany(self, statement: str, parameters: Any):
        cursor = self._connection.cursor()
        cursor.executemany(_postgres_sql(statement), parameters)
        return cursor

    def close(self) -> None:
        self._connection.close()


def connect() -> DatabaseConnection:
    options = _connection_options()
    if isinstance(options, dict):
        connection = psycopg.connect(**options, autocommit=True, row_factory=dict_row)
    else:
        connection = psycopg.connect(options, autocommit=True, row_factory=dict_row)
    connection.execute("SET TIME ZONE 'UTC'")
    return DatabaseConnection(connection)


@contextmanager
def database():
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        action TEXT NOT NULL DEFAULT 'provision',
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        plan_id TEXT,
        environment TEXT NOT NULL DEFAULT 'lab',
        instance_id TEXT,
        vmid BIGINT,
        target_node TEXT,
        secret_path TEXT,
        workspace_path TEXT,
        plan_path TEXT,
        plan_operation_id TEXT,
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_operations_heartbeat ON operations(status, last_heartbeat)",
    "CREATE INDEX IF NOT EXISTS idx_operations_instance_status ON operations(instance_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_operations_plan_id ON operations(plan_id)",
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id BIGSERIAL PRIMARY KEY,
        operation_id TEXT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_operation_id ON events(operation_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id)",
    """
    CREATE TABLE IF NOT EXISTS workers (
        worker_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        last_heartbeat TEXT NOT NULL,
        current_operation_id TEXT,
        started_at TEXT,
        heartbeat_count BIGINT NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workers_heartbeat ON workers(last_heartbeat)",
    """
    CREATE TABLE IF NOT EXISTS instances (
        instance_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        name TEXT NOT NULL,
        image TEXT NOT NULL,
        plan_id TEXT,
        vmid BIGINT,
        node TEXT NOT NULL DEFAULT 'mng01',
        environment TEXT NOT NULL DEFAULT 'lab',
        status TEXT NOT NULL,
        power_state TEXT,
        spec_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_instances_vmid ON instances(vmid)",
    "CREATE INDEX IF NOT EXISTS idx_instances_status_power ON instances(status, power_state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_instances_plan_id ON instances(plan_id)",
    """
    CREATE TABLE IF NOT EXISTS vmid_leases (
        vmid BIGINT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        released_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        response_json TEXT NOT NULL,
        status_code INTEGER NOT NULL,
        operation_id TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (scope, idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_idempotency_created_at ON idempotency_keys(created_at)",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'lab'",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'lab'",
)


def initialize() -> None:
    with database() as db:
        for statement in _SCHEMA:
            db.execute(statement)


def prune_history() -> dict[str, int]:
    """Compacta historial terminal solo si la retención se habilita explícitamente."""

    days = int(os.getenv("IACP_RETENTION_DAYS", "0"))
    if days <= 0:
        return {"events": 0, "operations": 0, "idempotency_keys": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with database() as db:
        db.execute("BEGIN")
        db.execute("SELECT pg_advisory_xact_lock(104, 10)")
        event_result = db.execute(
            """
            DELETE FROM events
            WHERE occurred_at < %s
              AND operation_id IN (
                SELECT operation_id FROM operations
                WHERE status IN ('succeeded', 'failed', 'uncertain')
                  AND finished_at IS NOT NULL AND finished_at < %s
              )
            """,
            (cutoff, cutoff),
        )
        idempotency_result = db.execute(
            """
            DELETE FROM idempotency_keys
            WHERE created_at < %s
              AND (
                operation_id IS NULL
                OR operation_id IN (
                  SELECT operation_id FROM operations
                  WHERE status IN ('succeeded', 'failed', 'uncertain')
                    AND finished_at IS NOT NULL AND finished_at < %s
                )
              )
            """,
            (cutoff, cutoff),
        )
        operation_result = db.execute(
            """
            DELETE FROM operations
            WHERE status IN ('succeeded', 'failed', 'uncertain')
              AND finished_at IS NOT NULL AND finished_at < %s
            """,
            (cutoff,),
        )
        db.execute("COMMIT")
    return {
        "events": event_result.rowcount,
        "operations": operation_result.rowcount,
        "idempotency_keys": idempotency_result.rowcount,
    }
