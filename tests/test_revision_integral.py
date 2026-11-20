"""Regresiones de la revisión integral del 2026-11-19."""

from __future__ import annotations

import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from api.db import _postgres_sql, database, initialize
from worker import main as worker_main


@pytest.mark.no_database
def test_traductor_sql_convierte_qmark_y_conserva_literales():
    assert _postgres_sql("SELECT ? , '?' , \"?\"") == "SELECT %s , '?' , \"?\""
    assert _postgres_sql("SELECT 'it''s ?' , ?") == "SELECT 'it''s ?' , %s"
    assert _postgres_sql("BEGIN IMMEDIATE") == "BEGIN"


@pytest.mark.no_database
def test_traductor_sql_escapa_porcentaje_literal_solo_con_parametros():
    assert _postgres_sql("SELECT 'a%b' WHERE x LIKE ?") == "SELECT 'a%%b' WHERE x LIKE %s"
    assert _postgres_sql("SELECT 'a%b'", escape_percent=False) == "SELECT 'a%b'"


def test_adaptador_acepta_porcentaje_literal_con_y_sin_parametros():
    with database() as db:
        assert db.execute("SELECT 'a%b' AS value").fetchone()["value"] == "a%b"
        row = db.execute("SELECT 'a%b' AS value, ? AS param", ("x",)).fetchone()
        assert row == {"value": "a%b", "param": "x"}
        like = db.execute("SELECT 'abc' LIKE 'a%' AS matched WHERE ? = ?", (1, 1)).fetchone()
        assert like["matched"] is True


@pytest.mark.no_database
def test_run_commands_no_se_bloquea_con_salida_mayor_que_el_pipe(monkeypatch, tmp_path):
    """Antes, >64 KiB en stdout/stderr bloqueaba al proceso hasta el timeout."""

    monkeypatch.setenv("IACP_TERRAFORM_TIMEOUT", "20")
    monkeypatch.setattr(worker_main, "heartbeat", lambda *args, **kwargs: None)
    script = "import sys; sys.stdout.write('o' * 400000); sys.stderr.write('e' * 400000)"

    started = time.monotonic()
    code, stdout, stderr = worker_main.run_commands(
        "worker-test", "operation-test", str(tmp_path), [[sys.executable, "-c", script]]
    )

    assert code == 0
    assert time.monotonic() - started < 10
    assert stdout.startswith("o") and stdout.endswith("o")
    assert stderr.startswith("e") and stderr.endswith("e")
    assert "[output truncated]" in stdout


@pytest.mark.no_database
def test_run_commands_redacta_secreto_en_salida_volcada_a_archivo(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_main, "heartbeat", lambda *args, **kwargs: None)
    secret = tmp_path / "secret"
    secret.write_text("ClaveDePrueba-123\n", encoding="utf-8")
    script = "import os, sys; print(os.environ['TF_VAR_root_password']); print('err ' + os.environ['TF_VAR_root_password'], file=sys.stderr)"

    code, stdout, stderr = worker_main.run_commands(
        "worker-test", "operation-test", str(tmp_path), [[sys.executable, "-c", script]],
        secret_path=str(secret),
    )

    assert code == 0
    assert "ClaveDePrueba-123" not in stdout + stderr
    assert "<redacted>" in stdout and "<redacted>" in stderr


def test_recuperacion_detecta_reconcile_por_campo_exacto_y_no_por_subcadena():
    initialize()
    with database() as db:
        db.execute(
            """
            INSERT INTO operations(operation_id, command, action, status, payload_json, created_at)
            VALUES ('rec-1', 'reconcile', 'reconcile', 'pending', ?, '2026-11-19T00:00:00+00:00')
            """,
            (json.dumps({"source_operation_id": "op-abc-extendida", "note": "op-xyz"}),),
        )
        assert worker_main._recovery_operation_exists(db, "op-abc-extendida") is True
        # Con LIKE '%op-abc%' ambos casos coincidían por subcadena.
        assert worker_main._recovery_operation_exists(db, "op-abc") is False
        assert worker_main._recovery_operation_exists(db, "op-xyz") is False


def test_token_bearer_se_valida_con_comparacion_constante(monkeypatch):
    monkeypatch.setenv("IACP_API_TOKEN", "token-de-prueba-completo")
    from api.main import app

    with TestClient(app) as client:
        assert client.get("/v1/plans").status_code == 401
        assert client.get("/v1/plans", headers={"Authorization": "Bearer token-de-prueba"}).status_code == 401
        assert client.get("/v1/plans", headers={"Authorization": "Bearer otro"}).status_code == 401
        ok = client.get("/v1/plans", headers={"Authorization": "Bearer token-de-prueba-completo"})
        assert ok.status_code == 200
