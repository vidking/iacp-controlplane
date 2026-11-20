"""Semántica del pool de conexiones PostgreSQL compartido por API y workers."""

import psycopg
import pytest

from api import db
from api.db import database, initialize


def _backend_pid(connection) -> int:
    return connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]


def test_pool_reutiliza_conexiones_entre_bloques():
    pids = set()
    for _ in range(20):
        with database() as connection:
            pids.add(_backend_pid(connection))
    # Bloques secuenciales: se reutilizan las conexiones del pool en vez de
    # abrir una por bloque (min_size más la abierta bajo demanda).
    assert len(pids) <= 2


def test_transaccion_sin_commit_se_revierte_al_devolver_la_conexion():
    initialize()
    with database() as connection:
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO workers(worker_id, status, last_heartbeat) VALUES ('pool-sin-commit', 'idle', 'x')"
        )
    with database() as connection:
        assert connection.execute(
            "SELECT 1 FROM workers WHERE worker_id='pool-sin-commit'"
        ).fetchone() is None
        status = connection._connection.info.transaction_status
        assert status == psycopg.pq.TransactionStatus.IDLE


def test_excepcion_dentro_de_transaccion_no_deja_estado_abierto():
    initialize()
    with pytest.raises(RuntimeError):
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO workers(worker_id, status, last_heartbeat) VALUES ('pool-error', 'idle', 'x')"
            )
            raise RuntimeError("fallo simulado")
    with database() as connection:
        assert connection.execute("SELECT 1 FROM workers WHERE worker_id='pool-error'").fetchone() is None
        # El advisory lock de BEGIN IMMEDIATE es de transacción y quedó liberado.
        locks = connection.execute(
            "SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()"
        ).fetchone()["n"]
        assert locks == 0


def test_pool_se_recupera_si_el_servidor_termina_la_conexion():
    with database() as connection:
        pid = _backend_pid(connection)
    admin = db.connect()
    try:
        admin.execute("SELECT pg_terminate_backend(?)", (pid,))
    finally:
        admin.close()
    with database() as connection:
        assert connection.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        assert _backend_pid(connection) != pid


def test_zona_horaria_utc_en_conexiones_del_pool():
    with database() as connection:
        assert connection.execute("SHOW TIME ZONE").fetchone()["TimeZone"] == "UTC"
