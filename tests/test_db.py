import pytest

from api.db import database


def _backend_pid(db) -> int:
    return db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]


def test_database_reuses_connection_within_thread():
    with database() as db:
        first = _backend_pid(db)
    with database() as db:
        second = _backend_pid(db)
    assert first == second


def test_database_discards_connection_after_error():
    with database() as db:
        first = _backend_pid(db)
    with pytest.raises(RuntimeError):
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            raise RuntimeError("fallo dentro de la transacción")
    with database() as db:
        assert _backend_pid(db) != first
        # La transacción abierta terminó en rollback al cerrar la conexión.
        assert db.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def test_database_discards_connection_left_in_transaction():
    with database() as db:
        db.execute("BEGIN")
        first = _backend_pid(db)
    with database() as db:
        assert _backend_pid(db) != first


def test_nested_database_uses_separate_connection():
    with database() as outer:
        outer_pid = _backend_pid(outer)
        with database() as inner:
            assert _backend_pid(inner) != outer_pid
        assert _backend_pid(outer) == outer_pid


def test_connection_reuse_can_be_disabled(monkeypatch):
    monkeypatch.setenv("IACP_DB_REUSE_CONNECTIONS", "0")
    with database() as db:
        first = _backend_pid(db)
    with database() as db:
        assert _backend_pid(db) != first
