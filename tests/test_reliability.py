"""Pruebas de confiabilidad: conexiones, fencing, apagado ordenado y caídas de PostgreSQL."""

import os
import time
from datetime import datetime, timezone

import psycopg
import pytest

from api.db import connect, database, initialize


def _insert_running_operation(operation_id: str, worker_id: str, instance_id: str = "fenced-instance") -> None:
    created = datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, status, payload_json, instance_id, worker_id, created_at)
            VALUES (?, 'provision', 'running', '{}', ?, ?, ?)
            """,
            (operation_id, instance_id, worker_id, created),
        )


def test_connection_sets_server_side_idle_in_transaction_timeout(monkeypatch):
    monkeypatch.setenv("IACP_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "500")
    connection = connect()
    try:
        value = connection.execute("SHOW idle_in_transaction_session_timeout").fetchone()
        assert value["idle_in_transaction_session_timeout"] == "500ms"
        connection.execute("BEGIN")
        time.sleep(1.5)
        # PostgreSQL terminó la sesión que quedó "idle in transaction".
        with pytest.raises(psycopg.OperationalError):
            connection.execute("SELECT 1")
    finally:
        connection.close()


def test_connection_identifies_role_in_pg_stat_activity(monkeypatch):
    monkeypatch.setenv("IACP_DB_APPLICATION_NAME", "iacp-worker-test")
    with database() as db:
        name = db.execute("SELECT current_setting('application_name') AS name").fetchone()["name"]
    assert name == "iacp-worker-test"


def test_cached_connection_recovers_after_backend_is_terminated(monkeypatch):
    """Simula un reinicio de PostgreSQL: el backend cacheado desaparece."""

    monkeypatch.setenv("IACP_DB_PING_IDLE_SECONDS", "0")
    with database() as db:
        pid = db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    admin = connect()
    try:
        admin.execute("SELECT pg_terminate_backend(?)", (pid,))
    finally:
        admin.close()
    time.sleep(0.2)
    with database() as db:
        assert db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] != pid


def test_stale_worker_cannot_overwrite_recovered_operation():
    import worker.main as worker_main

    initialize()
    _insert_running_operation("fenced-op", "worker-1")
    # recover_stale_operations() cerró la operación por heartbeat perdido.
    with database() as db:
        db.execute("UPDATE operations SET status='uncertain', phase='recovery_required' WHERE operation_id='fenced-op'")
    worker_main._fence.claim = ("fenced-op", "worker-1")
    try:
        with pytest.raises(worker_main.OperationFencedError):
            worker_main.update_operation("fenced-op", status="succeeded", phase="completed")
    finally:
        worker_main._fence.claim = None
    with database() as db:
        row = db.execute("SELECT status FROM operations WHERE operation_id='fenced-op'").fetchone()
    assert row["status"] == "uncertain"


def test_fenced_close_rolls_back_instance_and_lease_changes():
    import worker.main as worker_main
    from api.db import transaction

    initialize()
    created = datetime.now(timezone.utc).isoformat()
    _insert_running_operation("fenced-close", "worker-2", "fenced-close-instance")
    with database() as db:
        db.execute(
            """
            INSERT INTO instances(instance_id, operation_id, name, image, status, spec_json, created_at, updated_at)
            VALUES ('fenced-close-instance', 'fenced-close', 'fenced', 'ubuntu-2404', 'uncertain', '{}', ?, ?)
            """,
            (created, created),
        )
        db.execute("UPDATE operations SET worker_id='worker-7' WHERE operation_id='fenced-close'")
    worker_main._fence.claim = ("fenced-close", "worker-2")
    try:
        with pytest.raises(worker_main.OperationFencedError):
            with transaction():
                worker_main.update_operation("fenced-close", status="succeeded")
                worker_main.update_instance("fenced-close-instance", "ready")
    finally:
        worker_main._fence.claim = None
    with database() as db:
        status = db.execute("SELECT status FROM instances WHERE instance_id='fenced-close-instance'").fetchone()["status"]
    assert status == "uncertain"


def test_claimed_operation_can_still_be_written_by_its_owner():
    import worker.main as worker_main

    initialize()
    _insert_running_operation("owned-op", "worker-3")
    worker_main._fence.claim = ("owned-op", "worker-3")
    try:
        worker_main.update_operation("owned-op", phase="apply")
        worker_main.update_operation("owned-op", status="succeeded", phase="completed")
    finally:
        worker_main._fence.claim = None
    with database() as db:
        row = db.execute("SELECT status, phase FROM operations WHERE operation_id='owned-op'").fetchone()
    assert dict(row) == {"status": "succeeded", "phase": "completed"}


def test_run_loop_survives_database_outage_and_stops_gracefully(monkeypatch):
    import worker.main as worker_main

    initialize()
    calls = {"claim": 0}
    shutdown = worker_main._ShutdownFlag()

    def flaky_claim(_worker_id):
        calls["claim"] += 1
        if calls["claim"] == 1:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        shutdown.request()
        return None

    monkeypatch.setattr(worker_main, "claim", flaky_claim)
    monkeypatch.setattr(worker_main, "_db_retry_delay", lambda _failures: 0.0)
    monkeypatch.setenv("IACP_WORKER_POLL_SECONDS", "0.1")
    previous_umask = os.umask(0o022)
    os.umask(previous_umask)
    try:
        worker_main.run("worker-outage", shutdown=shutdown, max_loops=10)
    finally:
        os.umask(previous_umask)
    assert calls["claim"] == 2
    with database() as db:
        status = db.execute("SELECT status FROM workers WHERE worker_id='worker-outage'").fetchone()["status"]
    assert status == "stopped"


def test_run_loop_records_fenced_operation_and_continues(monkeypatch):
    import worker.main as worker_main

    initialize()
    shutdown = worker_main._ShutdownFlag()
    claimed = {"done": False}

    def one_claim(worker_id):
        if claimed["done"]:
            shutdown.request()
            return None
        claimed["done"] = True
        return {"operation_id": "fenced-run-op", "worker_id": worker_id}

    def fenced_execute(_worker_id, operation):
        raise worker_main.OperationFencedError(operation["operation_id"])

    monkeypatch.setattr(worker_main, "claim", one_claim)
    monkeypatch.setattr(worker_main, "execute", fenced_execute)
    previous_umask = os.umask(0o022)
    os.umask(previous_umask)
    try:
        worker_main.run("worker-fenced", shutdown=shutdown, max_loops=5)
    finally:
        os.umask(previous_umask)
    with database() as db:
        event = db.execute(
            "SELECT kind, status FROM events WHERE operation_id='fenced-run-op'"
        ).fetchone()
    assert dict(event) == {"kind": "operation.fenced", "status": "uncertain"}
