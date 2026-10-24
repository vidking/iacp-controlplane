from contextlib import contextmanager
import hashlib
from threading import Lock, RLock
from weakref import WeakKeyDictionary

from sqlalchemy import text

_guard = Lock()
_local = WeakKeyDictionary()
_admission = RLock()


@contextmanager
def admission_lock(session):
    """One short transaction serializes tenant quotas and cluster VMID reservations.

    PostgreSQL releases the lock at commit/rollback, including process death.
    Infrastructure work and broker calls must happen after this transaction.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(59173197781242)"))
        yield
    else:
        with _admission:
            yield


class ExecutionBusy(Exception):
    pass


@contextmanager
def instance_lock(engine, instance_id):
    if engine.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(instance_id.encode()).digest()[:8], "big", signed=True)
        with engine.connect() as connection:
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                raise ExecutionBusy()
            pid = connection.scalar(text("SELECT pg_backend_pid()"))
            def heartbeat():
                if connection.invalidated or connection.scalar(text("SELECT pg_backend_pid()")) != pid:
                    raise RuntimeError("Instance lock connection lost")
            try:
                yield heartbeat
            finally:
                # Los locks de sesión deben liberarse antes de devolver la conexión al pool.
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    else:
        # Solo las pruebas unitarias usan SQLite; el runtime configura PostgreSQL.
        with _guard:
            lock = _local.setdefault(engine, {}).setdefault(instance_id, Lock())
        if not lock.acquire(blocking=False):
            raise ExecutionBusy()
        try:
            yield lambda: None
        finally:
            lock.release()
