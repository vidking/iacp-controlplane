"""Controles de certificación: readiness, listados, PostgreSQL y Terraform."""

import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from api import db as db_module
from api import service
from api.db import database
from api.main import app
from tests.test_api import public_body
from worker import main as worker_main


def _count_queries(monkeypatch) -> list[str]:
    statements: list[str] = []
    original = db_module.DatabaseConnection.execute

    def counting(self, statement, parameters=None):
        statements.append(statement)
        return original(self, statement, parameters)

    monkeypatch.setattr(db_module.DatabaseConnection, "execute", counting)
    return statements


def test_readiness_no_ejecuta_ddl(monkeypatch):
    with TestClient(app) as client:
        def boom():
            raise AssertionError("readiness no debe ejecutar initialize()")

        monkeypatch.setattr("api.main.initialize", boom)
        statements = _count_queries(monkeypatch)
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert not any("CREATE" in sql.upper() or "ADVISORY" in sql.upper() for sql in statements)


def test_readiness_responde_503_sin_esquema():
    with TestClient(app) as client:
        with database() as db:
            db.execute("DELETE FROM schema_migrations")
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_listados_sin_n_mas_1_y_con_el_mismo_contenido(monkeypatch):
    with TestClient(app) as client:
        for index in range(12):
            assert client.post("/v1/instances", json=public_body(f"lista-{index:02d}")).status_code == 202
        individual = {
            row["instance_id"]: client.get(f"/v1/instances/{row['instance_id']}").json()
            for row in client.get("/v1/instances?limit=200").json()["instances"]
        }
        statements = _count_queries(monkeypatch)
        listing = client.get("/v1/instances?limit=200").json()
        instance_queries = len(statements)
        statements.clear()
        operations = client.get("/v1/operations?limit=200").json()
        operation_queries = len(statements)

    assert listing["total"] == 12
    for row in listing["instances"]:
        assert row == individual[row["instance_id"]]
    # COUNT + página + 3 prefetch, independiente del número de filas.
    assert instance_queries <= 5
    assert operation_queries <= 3
    assert all(operation["latest_event"] for operation in operations["operations"])
    created = [row["created_at"] for row in listing["instances"]]
    assert created == sorted(created, reverse=True)


def test_conexiones_con_keepalives_y_timeout_de_transaccion_inactiva():
    with database() as db:
        settings = {
            name: db.execute(f"SHOW {name}").fetchone()[name]
            for name in ("idle_in_transaction_session_timeout", "TimeZone", "application_name")
        }
    assert settings["idle_in_transaction_session_timeout"] == "1min"
    assert settings["TimeZone"] == "UTC"
    assert settings["application_name"].startswith("iacp-")
    assert "keepalives_idle" in db_module._pool_conninfo()


def test_terraform_no_hereda_credenciales_ajenas(monkeypatch):
    sensitive = {
        "PGPASSWORD": "pg",
        "IACP_DATABASE_URL": "postgresql://x",
        "IACP_API_TOKEN": "api",
        "IACP_NETBOX_TOKEN": "netbox",
        "IACP_PROXMOX_MNG_API_TOKEN": "duplicado",
        "AWS_SECRET_ACCESS_KEY": "s3",
        "WASABI_ACCESS_KEY": "w",
    }
    for name, value in sensitive.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PROXMOX_VE_API_TOKEN", "proxmox")
    monkeypatch.setenv("PROXMOX_VE_ENDPOINT", "https://mng01.test.invalid:8006/")
    monkeypatch.setenv("TF_VAR_extra", "1")
    environment = worker_main.terraform_environment()
    assert not set(sensitive) & set(environment)
    assert environment["PROXMOX_VE_API_TOKEN"] == "proxmox"
    assert environment["TF_VAR_extra"] == "1"
    assert environment["PATH"] == os.environ["PATH"]


def test_timeout_detiene_el_grupo_de_procesos_completo(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_KILL_GRACE_SECONDS", "1")
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
        f"open({str(child_pid_file)!r}, 'w').write(str(p.pid));"
        "time.sleep(120)"
    )
    process = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    deadline = time.monotonic() + 10
    while not child_pid_file.exists() or not child_pid_file.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    child_pid = int(child_pid_file.read_text())

    worker_main._terminate_process_group(process)

    assert process.poll() is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("el proceso hijo de Terraform quedó huérfano")


def test_heartbeat_de_comandos_acotado_y_tolerante(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_COMMAND_HEARTBEAT_SECONDS", "60")
    calls = []

    def flaky_heartbeat(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("PostgreSQL reiniciando")

    monkeypatch.setattr(worker_main, "heartbeat", flaky_heartbeat)
    monkeypatch.setattr(worker_main, "report", lambda *args: None)
    code, output, _ = worker_main.run_commands(
        "worker-1",
        "op-1",
        str(tmp_path),
        [[sys.executable, "-c", "import time; time.sleep(2.5); print('ok')"]],
    )
    assert code == 0
    assert "ok" in output
    # Un heartbeat al inicio y ninguno más dentro de la ventana de 60 s; el
    # fallo de PostgreSQL no aborta el comando.
    assert len(calls) == 1


def test_operation_row_conserva_latest_event_individual():
    with TestClient(app) as client:
        operation_id = client.post("/v1/instances", json=public_body("evento-unico")).json()["operation_id"]
        single = client.get(f"/v1/operations/{operation_id}").json()
    assert single["latest_event"]["operation_id"] == operation_id
    with database() as db:
        row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    assert service.operation_row(row, prefetched_event=None)["latest_event"] is None


def _running_operation(operation_id: str, worker_id: str = "worker-1") -> None:
    from api.db import initialize

    initialize()
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, stack, created_at, worker_id)
            VALUES (?, 'provision', 'provision', 'applying', '{}', 'mng01', ?, ?)
            """,
            (operation_id, worker_main.now(), worker_id),
        )


def test_fencing_rechaza_escrituras_tardias_tras_recuperacion():
    _running_operation("op-fence")
    token = worker_main._FENCE.set(("worker-1", "op-fence"))
    try:
        worker_main.update_operation("op-fence", phase="apply")
        with database() as db:
            db.execute(
                "INSERT INTO events(operation_id, kind, status, occurred_at) VALUES ('op-fence', 'recovery.required', 'uncertain', ?)",
                (worker_main.now(),),
            )
            db.execute("UPDATE operations SET status='uncertain' WHERE operation_id='op-fence'")
        with pytest.raises(worker_main.OperationFencedError):
            worker_main.update_operation("op-fence", status="succeeded", phase="completed")
        with pytest.raises(worker_main.OperationFencedError):
            worker_main.update_instance("inst-1", "ready")
    finally:
        worker_main._FENCE.reset(token)
    with database() as db:
        row = db.execute("SELECT status, phase FROM operations WHERE operation_id='op-fence'").fetchone()
        fenced_events = db.execute(
            "SELECT COUNT(*) AS count FROM events WHERE operation_id='op-fence' AND kind='operation.fenced'"
        ).fetchone()["count"]
    assert row["status"] == "uncertain" and row["phase"] == "apply"
    assert fenced_events == 2
    # Sin fence activo (reconciliación desde otro worker) la escritura aplica.
    worker_main.update_operation("op-fence", status="succeeded")


def test_fencing_no_afecta_otras_operaciones():
    _running_operation("op-propia")
    _running_operation("op-ajena", worker_id="worker-2")
    with database() as db:
        db.execute(
            "INSERT INTO events(operation_id, kind, status, occurred_at) VALUES ('op-ajena', 'recovery.required', 'uncertain', ?)",
            (worker_main.now(),),
        )
    token = worker_main._FENCE.set(("worker-1", "op-propia"))
    try:
        worker_main.update_operation("op-ajena", phase="reconciled")
        worker_main.update_operation("op-propia", status="succeeded")
    finally:
        worker_main._FENCE.reset(token)
    with database() as db:
        assert db.execute("SELECT status FROM operations WHERE operation_id='op-propia'").fetchone()["status"] == "succeeded"
        assert db.execute("SELECT phase FROM operations WHERE operation_id='op-ajena'").fetchone()["phase"] == "reconciled"


def test_healthcheck_de_worker_por_heartbeat(monkeypatch):
    from worker import healthcheck
    from worker.heartbeat import heartbeat

    _running_operation("op-hc")
    assert healthcheck.check("worker-9")[0] is False
    heartbeat("worker-9", "idle")
    assert healthcheck.check("worker-9")[0] is True
    with database() as db:
        db.execute("UPDATE workers SET last_heartbeat='2000-01-01T00:00:00+00:00' WHERE worker_id='worker-9'")
    healthy, detail = healthcheck.check("worker-9")
    assert healthy is False and "obsoleto" in detail


@pytest.mark.no_database
def test_healthcheck_detecta_worker_id_en_proc(tmp_path):
    from worker.healthcheck import detect_worker_id

    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "cmdline").write_bytes(b"docker-init\0--\0")
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "cmdline").write_bytes(b"python\0-m\0worker.main\0--worker-id\0worker-4\0")
    assert detect_worker_id(tmp_path) == "worker-4"


@pytest.mark.no_database
def test_stop_request_deja_de_reclamar(monkeypatch):
    from worker import resilience

    claims = []
    monkeypatch.setattr(worker_main, "initialize", lambda: None)
    monkeypatch.setattr(worker_main, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(worker_main, "recover_stale_operations", lambda: 0)

    def claim(worker_id):
        claims.append(worker_id)
        resilience.STOP.requested = True
        return None

    monkeypatch.setattr(worker_main, "claim", claim)
    monkeypatch.setattr(worker_main.time, "sleep", lambda _: None)
    try:
        worker_main.run("worker-1", iterations=50)
    finally:
        resilience.STOP.requested = False
    assert claims == ["worker-1"]
