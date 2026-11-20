"""Los bucles de los workers contienen errores transitorios en vez de terminar."""

import psycopg
import pytest

from worker import main as worker_main
from worker import services as service_worker
from worker.resilience import Backoff

pytestmark = pytest.mark.no_database


def test_backoff_crece_con_tope_y_se_reinicia():
    backoff = Backoff(0.5, 4.0)
    assert [backoff.failure() for _ in range(5)] == [1.0, 2.0, 4.0, 4.0, 4.0]
    backoff.success()
    assert backoff.failure() == 1.0


def test_worker_sobrevive_a_un_fallo_transitorio_del_claim(monkeypatch, capsys):
    calls = {"claim": 0, "heartbeat": []}
    monkeypatch.setattr(worker_main, "initialize", lambda: None)
    monkeypatch.setattr(worker_main, "recover_stale_operations", lambda: 0)
    monkeypatch.setattr(worker_main, "heartbeat", lambda worker_id, status, *a, **k: calls["heartbeat"].append(status))
    monkeypatch.setattr(worker_main.time, "sleep", lambda _seconds: None)

    def claim(_worker_id):
        calls["claim"] += 1
        if calls["claim"] == 1:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return None

    monkeypatch.setattr(worker_main, "claim", claim)
    worker_main.run("worker-test", iterations=3)

    assert calls["claim"] == 3
    assert "starting" in calls["heartbeat"] and "idle" in calls["heartbeat"]
    assert "error transitorio en claim" in capsys.readouterr().err


def test_worker_reintenta_la_inicializacion_hasta_que_postgres_responde(monkeypatch):
    attempts = {"n": 0}

    def initialize():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(worker_main, "initialize", initialize)
    monkeypatch.setattr(worker_main, "recover_stale_operations", lambda: 0)
    monkeypatch.setattr(worker_main, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(worker_main, "claim", lambda _worker_id: None)
    monkeypatch.setattr(worker_main.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("worker.resilience.time.sleep", lambda _seconds: None)

    worker_main.run("worker-test", iterations=1)
    assert attempts["n"] == 3


def test_service_worker_sobrevive_a_un_fallo_de_recuperacion(monkeypatch, capsys):
    calls = {"recover": 0, "claim": 0}
    monkeypatch.setattr(service_worker, "initialize", lambda: None)
    monkeypatch.setattr(service_worker.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(service_worker, "heartbeat", lambda *a, **k: None)

    def recover():
        calls["recover"] += 1
        if calls["recover"] == 1:
            raise psycopg.OperationalError("terminating connection due to administrator command")

    def claim(_worker_id):
        calls["claim"] += 1
        return None

    monkeypatch.setattr(service_worker, "recover", recover)
    monkeypatch.setattr(service_worker, "claim", claim)
    service_worker.run("service-worker-test", iterations=2)

    assert calls == {"recover": 2, "claim": 1}
    assert "error transitorio en recuperación" in capsys.readouterr().err


def test_update_operation_rechaza_columnas_fuera_de_la_lista():
    with pytest.raises(ValueError, match="no permitidas"):
        worker_main.update_operation("op", **{"status='x'; DROP TABLE operations; --": 1})


def test_credenciales_de_servicio_usan_csprng_con_el_mismo_formato():
    from api.service_secrets import installation_credentials

    first, second = installation_credentials(), installation_credentials()
    for key in ("admin_password", "database_password", "encryption_key", "gateway_token"):
        assert len(first[key]) == 64 and int(first[key], 16) >= 0
        assert first[key] != second[key]
