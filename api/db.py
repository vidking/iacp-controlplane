"""Persistencia PostgreSQL compartida por la API y los workers.

La aplicación comienza con una base vacía. Este módulo no importa registros
históricos de SQLite ni datos del runtime anterior.
"""

from __future__ import annotations

import os
import threading
import time
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
        # SQLite BEGIN IMMEDIATE serializaba los claims y la asignación de
        # VMID. Reproducimos esa sección crítica entre procesos PostgreSQL
        # con un advisory transaction lock; sin él, seis workers podrían
        # reclamar de más o reservar el mismo VMID.
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            self._connection.execute("BEGIN")
            return self._connection.execute("SELECT pg_advisory_xact_lock(104, 101)")
        return self._connection.execute(_postgres_sql(statement), parameters)

    def executemany(self, statement: str, parameters: Any):
        cursor = self._connection.cursor()
        cursor.executemany(_postgres_sql(statement), parameters)
        return cursor

    def close(self) -> None:
        self._connection.close()


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _session_parameters() -> dict[str, Any]:
    """Parámetros de sesión y de transporte aplicados a cada conexión.

    - ``idle_in_transaction_session_timeout``: PostgreSQL termina una sesión
      que deja una transacción abierta sin actividad (por ejemplo, un worker
      colgado dentro de ``BEGIN``), liberando locks y el advisory lock global.
    - TCP keepalives: detectan una conexión muerta tras un reinicio de red o
      de PostgreSQL en lugar de esperar indefinidamente.
    - ``application_name``: identifica API y workers en ``pg_stat_activity``.
    """

    idle_tx_ms = _int_env("IACP_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 60000)
    options = f"-c idle_in_transaction_session_timeout={idle_tx_ms}"
    statement_ms = _int_env("IACP_DB_STATEMENT_TIMEOUT_MS", 0)
    if statement_ms:
        options += f" -c statement_timeout={statement_ms}"
    return {
        "application_name": (os.getenv("IACP_DB_APPLICATION_NAME") or "iacp-controlplane")[:63],
        "keepalives": 1,
        "keepalives_idle": _int_env("IACP_DB_KEEPALIVES_IDLE", 30, 1),
        "keepalives_interval": _int_env("IACP_DB_KEEPALIVES_INTERVAL", 10, 1),
        "keepalives_count": _int_env("IACP_DB_KEEPALIVES_COUNT", 3, 1),
        "options": options,
    }


def connect() -> DatabaseConnection:
    options = _connection_options()
    session = _session_parameters()
    if isinstance(options, dict):
        connection = psycopg.connect(**options, **session, autocommit=True, row_factory=dict_row)
    else:
        # Los parámetros explícitos se combinan con la URL; si la URL ya
        # define "options" (por ejemplo search_path en tests), se conservan
        # ambos.
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        existing = conninfo_to_dict(options).get("options")
        if existing:
            session["options"] = f"{existing} {session['options']}"
        connection = psycopg.connect(make_conninfo(options, **session), autocommit=True, row_factory=dict_row)
    connection.execute("SET TIME ZONE 'UTC'")
    return DatabaseConnection(connection)


_local = threading.local()


def _reuse_enabled() -> bool:
    return os.getenv("IACP_DB_REUSE_CONNECTIONS", "1").strip().lower() not in {"0", "false", "no", "off"}


def _max_connection_age() -> float:
    try:
        return max(1.0, float(os.getenv("IACP_DB_CONNECTION_MAX_AGE_SECONDS", "300")))
    except ValueError:
        return 300.0


def _ping_idle_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("IACP_DB_PING_IDLE_SECONDS", "30")))
    except ValueError:
        return 30.0


def _ping(connection: DatabaseConnection) -> bool:
    try:
        connection._connection.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


def close_thread_connection() -> None:
    """Cierra la conexión reutilizable del hilo actual (apagado ordenado)."""

    _discard_cached()


def _discard_cached() -> None:
    cached = getattr(_local, "connection", None)
    _local.connection = None
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass


@contextmanager
def database():
    """Entrega una conexión PostgreSQL en autocommit.

    Cada hilo reutiliza una conexión mientras siga sana, apunte al mismo
    destino y no supere IACP_DB_CONNECTION_MAX_AGE_SECONDS. Antes se abría
    una conexión TCP nueva (más ``SET TIME ZONE``) por cada sentencia.

    La conexión se descarta (como antes, con ``close()``) si el bloque
    lanza una excepción o deja una transacción abierta, de modo que un
    ``BEGIN`` sin ``COMMIT`` sigue terminando en rollback. Un uso anidado
    recibe una conexión propia, igual que el comportamiento anterior.
    """

    active = getattr(_local, "transaction", None)
    if active is not None:
        # Dentro de transaction() todas las escrituras del hilo comparten la
        # misma transacción; el cierre lo controla transaction().
        yield active
        return

    if not _reuse_enabled() or getattr(_local, "depth", 0) > 0:
        connection = connect()
        try:
            yield connection
        finally:
            connection.close()
        return

    key = repr(_connection_options())
    cached = getattr(_local, "connection", None)
    raw = cached._connection if cached is not None else None
    if (
        cached is not None
        and not raw.closed
        and not raw.broken
        and time.monotonic() - getattr(_local, "last_used", 0.0) > _ping_idle_seconds()
        and not _ping(cached)
    ):
        # Tras un reinicio de PostgreSQL la conexión cacheada parece abierta
        # hasta el primer uso; un ping barato tras un periodo inactivo evita
        # entregar al llamador una conexión muerta.
        _discard_cached()
        cached = None
    if (
        cached is None
        or raw.closed
        or raw.broken
        or getattr(_local, "key", None) != key
        or time.monotonic() - getattr(_local, "opened_at", 0.0) > _max_connection_age()
    ):
        _discard_cached()
        cached = connect()
        _local.connection = cached
        _local.key = key
        _local.opened_at = time.monotonic()

    _local.depth = 1
    clean = False
    try:
        yield cached
        clean = True
    finally:
        _local.depth = 0
        _local.last_used = time.monotonic()
        raw = cached._connection
        if (
            not clean
            or raw.closed
            or raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE
        ):
            _discard_cached()


class _TransactionConnection:
    """Conexión compartida por transaction(); rechaza control transaccional anidado."""

    def __init__(self, connection: DatabaseConnection):
        self._inner = connection

    def execute(self, statement: str, parameters: Any = ()):
        if statement.strip().upper() in {"BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}:
            raise RuntimeError("control transaccional anidado dentro de transaction()")
        return self._inner.execute(statement, parameters)

    def executemany(self, statement: str, parameters: Any):
        return self._inner.executemany(statement, parameters)

    def close(self) -> None:
        # transaction() es dueña del ciclo de vida.
        pass


@contextmanager
def transaction():
    """Agrupa en una sola transacción las escrituras del hilo.

    Las funciones que abren ``database()`` dentro del bloque reutilizan esta
    conexión, de modo que un cierre de operación (operación, instancia,
    lease y evento) se confirma completo o no se confirma. Un fallo hace
    rollback y descarta la conexión.
    """

    if getattr(_local, "transaction", None) is not None:
        yield _local.transaction
        return
    with database() as db:
        db.execute("BEGIN")
        _local.transaction = _TransactionConnection(db)
        try:
            yield _local.transaction
        except BaseException:
            _local.transaction = None
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            raise
        else:
            _local.transaction = None
            db.execute("COMMIT")


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
    "CREATE INDEX IF NOT EXISTS idx_idempotency_created_at ON idempotency_keys(created_at)",
    """
    CREATE TABLE IF NOT EXISTS ip_allocations (
        allocation_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'netbox',
        profile TEXT NOT NULL,
        prefix_id BIGINT NOT NULL,
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
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_ip_allocations_active_instance
    ON ip_allocations(instance_id, profile)
    WHERE status IN ('allocating', 'allocated', 'release_pending')
    """,
    "CREATE INDEX IF NOT EXISTS idx_ip_allocations_operation ON ip_allocations(operation_id, status)",
    # Índices aditivos para listados sin filtro, el chequeo de nombre
    # duplicado y la búsqueda de reconciliaciones existentes.
    "CREATE INDEX IF NOT EXISTS idx_operations_created_at ON operations(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_instances_created_at ON instances(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_instances_active_name ON instances(name) WHERE status <> 'deleted'",
    "CREATE INDEX IF NOT EXISTS idx_operations_reconcile ON operations(created_at) WHERE command = 'reconcile'",
    "CREATE INDEX IF NOT EXISTS idx_operations_worker_created ON operations(worker_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
)


# scripts/go_live.py exige schema_version == 2 como contrato V1; los índices
# aditivos con IF NOT EXISTS no cambian esa versión.
SCHEMA_VERSION = 2


def initialize() -> None:
    with database() as db:
        for statement in _SCHEMA:
            db.execute(statement)
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) ON CONFLICT(version) DO NOTHING",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )


def schema_ready() -> bool:
    """Comprueba conexión y esquema sin ejecutar DDL.

    ``initialize()`` toma locks exclusivos en las tablas aunque no cambie
    nada; por eso la readiness solo lee ``schema_migrations``.
    """

    with database() as db:
        table = db.execute("SELECT to_regclass('schema_migrations') AS name").fetchone()
        if table is None or table["name"] is None:
            return False
        row = db.execute(
            "SELECT 1 AS ok FROM schema_migrations WHERE version = ? LIMIT 1",
            (SCHEMA_VERSION,),
        ).fetchone()
    return row is not None


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
