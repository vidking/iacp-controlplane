"""Persistencia PostgreSQL compartida por la API y los workers.

La aplicación comienza con una base vacía. Este módulo no importa registros
históricos de SQLite ni datos del runtime anterior.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


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


def _postgres_sql(statement: str, *, escape_percent: bool = True) -> str:
    """Traduce los parámetros qmark del repositorio al formato de psycopg.

    Con parámetros, psycopg interpreta cada ``%`` como marcador; por eso un
    ``%`` literal (por ejemplo, en ``LIKE 'abc%'``) se escapa como ``%%``.
    Los ``?`` dentro de literales entre comillas se conservan.
    """

    if statement.strip().upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    output: list[str] = []
    quote: str | None = None
    for character in statement:
        if character == "%" and escape_percent:
            output.append("%%")
            continue
        if quote:
            output.append(character)
            if character == quote:
                # SQL estándar duplica la comilla para escaparla (''); el
                # cierre y la reapertura inmediata producen el mismo texto.
                quote = None
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

    def execute(self, statement: str, parameters: Any = None):
        # SQLite BEGIN IMMEDIATE serializaba los claims y la asignación de
        # VMID. Reproducimos esa sección crítica entre procesos PostgreSQL
        # con un advisory transaction lock; sin él, seis workers podrían
        # reclamar de más o reservar el mismo VMID.
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            self._connection.execute("BEGIN")
            return self._connection.execute("SELECT pg_advisory_xact_lock(104, 101)")
        if not parameters:
            # Sin parámetros psycopg no interpreta marcadores; un ``%``
            # literal debe llegar intacto al servidor.
            return self._connection.execute(_postgres_sql(statement, escape_percent=False))
        return self._connection.execute(_postgres_sql(statement), parameters)

    def executemany(self, statement: str, parameters: Any):
        cursor = self._connection.cursor()
        cursor.executemany(_postgres_sql(statement), parameters)
        return cursor

    def close(self) -> None:
        self._connection.close()


def connect() -> DatabaseConnection:
    """Conexión dedicada fuera del pool (diagnóstico y herramientas)."""

    options = _connection_options()
    if isinstance(options, dict):
        connection = psycopg.connect(**options, autocommit=True, row_factory=dict_row)
    else:
        connection = psycopg.connect(options, autocommit=True, row_factory=dict_row)
    connection.execute("SET TIME ZONE 'UTC'")
    return DatabaseConnection(connection)


def _env_number(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _session_options() -> dict[str, str]:
    """Parámetros libpq de resiliencia que no sobrescriben los explícitos.

    Los keepalives TCP detectan una conexión muerta (reinicio o corte de red
    de PostgreSQL) sin esperar al timeout del kernel; ``application_name``
    permite identificar en ``pg_stat_activity`` qué proceso retiene un lock.
    """

    return {
        "keepalives": "1",
        "keepalives_idle": str(int(_env_number("IACP_DB_KEEPALIVES_IDLE", 30, 1))),
        "keepalives_interval": str(int(_env_number("IACP_DB_KEEPALIVES_INTERVAL", 10, 1))),
        "keepalives_count": str(int(_env_number("IACP_DB_KEEPALIVES_COUNT", 3, 1))),
        "application_name": os.getenv("IACP_DB_APPLICATION_NAME", "").strip()
        or f"iacp-{os.getenv('IACP_WORKER_ID', 'api')}"[:63],
    }


def _pool_conninfo() -> str:
    options = _connection_options()
    if isinstance(options, dict):
        base = make_conninfo(**{key: str(value) for key, value in options.items()})
    elif "connect_timeout" in options:
        base = options
    else:
        base = make_conninfo(
            options, connect_timeout=str(int(_env_number("IACP_DB_CONNECT_TIMEOUT", 5, 1)))
        )
    present = set(conninfo_to_dict(base))
    extra = {key: value for key, value in _session_options().items() if key not in present}
    return make_conninfo(base, **extra) if extra else base


def _configure_connection(connection: psycopg.Connection) -> None:
    connection.execute("SET TIME ZONE 'UTC'")
    # Una transacción olvidada (excepción entre BEGIN y COMMIT en un proceso
    # que no vuelve a usar la conexión) retendría el advisory lock global y
    # bloquearía claims y asignación de VMID de todos los workers.
    idle_ms = int(_env_number("IACP_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 60000, 0))
    connection.execute(f"SET idle_in_transaction_session_timeout = {idle_ms}")
    statement_ms = int(_env_number("IACP_DB_STATEMENT_TIMEOUT_MS", 0, 0))
    if statement_ms:
        connection.execute(f"SET statement_timeout = {statement_ms}")


_POOL: ConnectionPool | None = None
_POOL_KEY: tuple | None = None
_POOL_LOCK = threading.Lock()


def _pool() -> ConnectionPool:
    """Pool por proceso; se recrea si cambia la configuración de conexión.

    Antes cada consulta abría una conexión nueva (autenticación y SET TIME
    ZONE incluidos): un listado de 50 instancias abría 50 conexiones y cada
    heartbeat de los diez workers abría otra.
    """

    global _POOL, _POOL_KEY
    conninfo = _pool_conninfo()
    key = (os.getpid(), conninfo)
    with _POOL_LOCK:
        if _POOL is not None and _POOL_KEY == key:
            return _POOL
        previous = _POOL
        _POOL = ConnectionPool(
            conninfo,
            min_size=int(_env_number("IACP_DB_POOL_MIN_SIZE", 1, 0)),
            max_size=int(_env_number("IACP_DB_POOL_MAX_SIZE", 5, 1)),
            kwargs={"autocommit": True, "row_factory": dict_row},
            configure=_configure_connection,
            # Valida cada conexión antes de entregarla: un reinicio de
            # PostgreSQL no debe propagarse como error a la primera consulta.
            check=ConnectionPool.check_connection,
            timeout=_env_number("IACP_DB_POOL_TIMEOUT", 10, 1),
            max_idle=_env_number("IACP_DB_POOL_MAX_IDLE", 300, 30),
            max_lifetime=_env_number("IACP_DB_POOL_MAX_LIFETIME", 1800, 60),
            name="iacp",
            open=True,
        )
        _POOL_KEY = key
    if previous is not None and previous.name == "iacp":
        try:
            previous.close(timeout=5)
        except Exception:
            pass
    return _POOL


def close_pool() -> None:
    global _POOL, _POOL_KEY
    with _POOL_LOCK:
        pool, _POOL, _POOL_KEY = _POOL, None, None
    if pool is not None:
        pool.close(timeout=5)


@contextmanager
def database():
    pool = _pool()
    connection = pool.getconn()
    try:
        yield DatabaseConnection(connection)
    finally:
        # Misma semántica que cerrar una conexión dedicada: una transacción
        # que no llegó a COMMIT se revierte y no se reutiliza su estado.
        try:
            if not connection.closed and connection.info.transaction_status != TransactionStatus.IDLE:
                connection.rollback()
        except Exception:
            pass
        pool.putconn(connection)


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        action TEXT NOT NULL DEFAULT 'provision',
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        plan_id TEXT,
        stack TEXT NOT NULL DEFAULT 'mng01',
        commercial_context_json TEXT NOT NULL DEFAULT '{}',
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
        commercial_context_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
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
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS stack TEXT NOT NULL DEFAULT 'mng01'",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS action TEXT NOT NULL DEFAULT 'provision'",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS plan_id TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS instance_id TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS vmid BIGINT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS workspace_path TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS plan_path TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS plan_operation_id TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS target_node TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS secret_path TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS commercial_context_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'queued'",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS started_at TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS finished_at TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS last_heartbeat TEXT",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS exit_code INTEGER",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS output TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS error TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE operations ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'lab'",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS plan_id TEXT",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS commercial_context_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'lab'",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS power_state TEXT",
    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS started_at TEXT",
    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS heartbeat_count BIGINT NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_operations_heartbeat ON operations(status, last_heartbeat)",
    "CREATE INDEX IF NOT EXISTS idx_operations_instance_status ON operations(instance_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_operations_plan_id ON operations(plan_id)",
    "CREATE INDEX IF NOT EXISTS idx_workers_heartbeat ON workers(last_heartbeat)",
    "CREATE INDEX IF NOT EXISTS idx_instances_vmid ON instances(vmid)",
    "CREATE INDEX IF NOT EXISTS idx_instances_status_power ON instances(status, power_state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_instances_plan_id ON instances(plan_id)",
    # Índices aditivos para listados paginados y prefetch de los listados.
    "CREATE INDEX IF NOT EXISTS idx_instances_created_at ON instances(created_at DESC, instance_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_operations_created_at ON operations(created_at DESC, operation_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_instances_active_name ON instances(name) WHERE status <> 'deleted'",
    "CREATE INDEX IF NOT EXISTS idx_idempotency_created_at ON idempotency_keys(created_at)",
    """
    CREATE TABLE IF NOT EXISTS ip_allocations (
        allocation_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'netbox',
        profile TEXT NOT NULL,
        prefix_id BIGINT,
        netbox_ip_id BIGINT,
        address TEXT NOT NULL DEFAULT '',
        gateway TEXT NOT NULL DEFAULT '',
        marker TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        released_at TEXT,
        last_error TEXT NOT NULL DEFAULT ''
    )
    """,
    "ALTER TABLE ip_allocations ALTER COLUMN prefix_id DROP NOT NULL",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_ip_allocations_active_instance
    ON ip_allocations(instance_id, profile)
    WHERE status IN ('allocating', 'allocated', 'release_pending')
    """,
    "CREATE INDEX IF NOT EXISTS idx_ip_allocations_operation ON ip_allocations(operation_id, status)",
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
)


SCHEMA_VERSION = 3


def schema_ready() -> bool:
    """Readiness de solo lectura: no ejecuta DDL ni toma locks.

    ``initialize()`` ejecuta unas 60 sentencias DDL bajo el advisory lock
    global; llamarlo desde ``/health/ready`` cada 10 s competía con los
    claims y la asignación de VMID de los workers.
    """

    with database() as db:
        row = db.execute(
            "SELECT 1 AS ok FROM schema_migrations WHERE version >= ? LIMIT 1",
            (SCHEMA_VERSION,),
        ).fetchone()
    return row is not None


def initialize() -> None:
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA:
            db.execute(statement)
        from api.service_schema import SCHEMA
        for statement in SCHEMA:
            db.execute(statement)
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) ON CONFLICT(version) DO NOTHING",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )
        db.execute("COMMIT")


def prune_history() -> dict[str, int]:
    """Compacta historial terminal solo si la retención se habilita explícitamente."""

    days = int(os.getenv("IACP_RETENTION_DAYS", "0"))
    if days <= 0:
        return {"events": 0, "operations": 0, "idempotency_keys": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with database() as db:
        # Serializa la selección con el encolado y la asignación de nuevas
        # referencias. Los recursos vigentes conservan sus operaciones raíz.
        db.execute("BEGIN IMMEDIATE")
        result = db.execute(
            """
            WITH source_references AS MATERIALIZED (
                SELECT plan_operation_id AS operation_id FROM operations
                WHERE plan_operation_id IS NOT NULL
                UNION
                SELECT payload_json::jsonb ->> 'source_operation_id'
                FROM operations
                WHERE command = 'reconcile' AND payload_json IS JSON OBJECT
            ), eligible AS MATERIALIZED (
                SELECT candidate.operation_id
                FROM operations candidate
                WHERE candidate.status IN ('succeeded', 'failed')
                  AND candidate.finished_at IS NOT NULL AND candidate.finished_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM instances i
                      WHERE i.operation_id = candidate.operation_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM operations other
                      WHERE other.instance_id = candidate.instance_id
                        AND other.status NOT IN ('succeeded', 'failed')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM source_references ref
                      WHERE ref.operation_id = candidate.operation_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM events event
                      WHERE event.operation_id = candidate.operation_id AND event.occurred_at >= ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM idempotency_keys key
                      WHERE key.operation_id = candidate.operation_id AND key.created_at >= ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM vmid_leases lease
                      WHERE lease.operation_id = candidate.operation_id AND lease.status = 'held'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM ip_allocations allocation
                      WHERE allocation.operation_id = candidate.operation_id AND allocation.status <> 'released'
                  )
            ), removed_events AS (
                DELETE FROM events WHERE operation_id IN (SELECT operation_id FROM eligible)
                RETURNING event_id
            ), removed_keys AS (
                DELETE FROM idempotency_keys
                WHERE operation_id IN (SELECT operation_id FROM eligible)
                RETURNING 1
            ), removed_operations AS (
                DELETE FROM operations WHERE operation_id IN (SELECT operation_id FROM eligible)
                RETURNING operation_id
            )
            SELECT (SELECT COUNT(*) FROM removed_events) AS events,
                   (SELECT COUNT(*) FROM removed_operations) AS operations,
                   (SELECT COUNT(*) FROM removed_keys) AS idempotency_keys
            """,
            (cutoff, cutoff, cutoff),
        ).fetchone()
        db.execute("COMMIT")
    return dict(result)
