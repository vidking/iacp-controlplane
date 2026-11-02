"""Persistencia PostgreSQL del IACP Control Plane.

La API pública utiliza el contrato ``/v1``. PostgreSQL es la única fuente
persistente de operaciones, leases, inventario y eventos. La base se inicializa
vacía para esta migración; no se importan datos ni VMs del runtime anterior.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row


def database_url() -> str:
    value = (
        os.getenv("IACP_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_DSN")
        or ""
    ).strip()
    if not value:
        raise RuntimeError("IACP_DATABASE_URL es obligatorio; PostgreSQL es la persistencia oficial")
    return value


def _sql(statement: str) -> str:
    """Convertir los marcadores de parámetros al formato de psycopg."""

    return statement.replace("?", "%s")


class DatabaseConnection:
    """Adaptador mínimo para consultas del API y de los workers."""

    def __init__(self, connection: psycopg.Connection):
        self._connection = connection

    def execute(self, statement: str, params: Iterable[object] | None = None):
        return self._connection.execute(_sql(statement), tuple(params or ()))

    def executemany(self, statement: str, params: Iterable[Iterable[object]]):
        cursor = self._connection.cursor()
        cursor.executemany(_sql(statement), params)
        return cursor

    def begin(self) -> None:
        """Iniciar una transacción si una consulta previa no la abrió ya."""

        if self._connection.info.transaction_status == TransactionStatus.IDLE:
            self._connection.execute("BEGIN")

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def connect() -> DatabaseConnection:
    connection = psycopg.connect(
        database_url(),
        row_factory=dict_row,
        connect_timeout=int(os.getenv("IACP_DATABASE_CONNECT_TIMEOUT", "10")),
        application_name=os.getenv("IACP_DATABASE_APPLICATION_NAME", "iacp-controlplane"),
    )
    return DatabaseConnection(connection)


@contextmanager
def database():
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        action TEXT NOT NULL DEFAULT 'provision',
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        plan_id TEXT,
        commercial_context_json TEXT NOT NULL DEFAULT '{}',
        secret_path TEXT,
        instance_id TEXT,
        plan_operation_id TEXT,
        stack TEXT NOT NULL DEFAULT 'mng01',
        vmid INTEGER,
        workspace_path TEXT,
        plan_path TEXT,
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_operations_heartbeat ON operations(status, last_heartbeat)",
    "CREATE INDEX IF NOT EXISTS idx_operations_instance_status ON operations(instance_id, status, created_at)",
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
        heartbeat_count INTEGER NOT NULL DEFAULT 0
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
        vmid INTEGER,
        node TEXT NOT NULL DEFAULT 'mng01',
        status TEXT NOT NULL,
        power_state TEXT,
        spec_json TEXT NOT NULL,
        commercial_context_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_active_name ON instances(name) WHERE status <> 'deleted'",
    "CREATE INDEX IF NOT EXISTS idx_instances_vmid ON instances(vmid)",
    "CREATE INDEX IF NOT EXISTS idx_instances_plan_id ON instances(plan_id)",
    "CREATE INDEX IF NOT EXISTS idx_instances_status_power ON instances(status, power_state, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS vmid_leases (
        vmid INTEGER PRIMARY KEY,
        operation_id TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        released_at TEXT
    )
    """,
    """
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
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_ip_allocations_active_instance
      ON ip_allocations(instance_id, profile)
      WHERE status IN ('allocating', 'allocated', 'release_pending')
    """,
    "CREATE INDEX IF NOT EXISTS idx_ip_allocations_operation ON ip_allocations(operation_id, status)",
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
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)


def initialize() -> None:
    """Crear el esquema limpio de la línea nueva de Control Plane."""

    with database() as db:
        for statement in SCHEMA:
            db.execute(statement)
        db.execute(
            """
            INSERT INTO schema_meta(key, value, updated_at)
            VALUES ('schema_version', '1', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )


def prune_history() -> dict[str, int]:
    """Compactar historial terminal únicamente cuando se habilita explícitamente."""

    days = int(os.getenv("IACP_RETENTION_DAYS", "0"))
    if days <= 0:
        return {"events": 0, "operations": 0, "idempotency_keys": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with database() as db:
        db.begin()
        event_result = db.execute(
            """
            DELETE FROM events
            WHERE occurred_at < ?
              AND operation_id IN (
                SELECT operation_id FROM operations
                WHERE status IN ('succeeded', 'failed', 'uncertain')
                  AND finished_at IS NOT NULL AND finished_at < ?
              )
            """,
            (cutoff, cutoff),
        )
        idempotency_result = db.execute(
            """
            DELETE FROM idempotency_keys
            WHERE created_at < ?
              AND (
                operation_id IS NULL OR operation_id IN (
                  SELECT operation_id FROM operations
                  WHERE status IN ('succeeded', 'failed', 'uncertain')
                    AND finished_at IS NOT NULL AND finished_at < ?
                )
              )
            """,
            (cutoff, cutoff),
        )
        operation_result = db.execute(
            """
            DELETE FROM operations
            WHERE status IN ('succeeded', 'failed', 'uncertain')
              AND finished_at IS NOT NULL AND finished_at < ?
            """,
            (cutoff,),
        )
        db.execute("COMMIT")
    return {
        "events": event_result.rowcount,
        "operations": operation_result.rowcount,
        "idempotency_keys": idempotency_result.rowcount,
    }
