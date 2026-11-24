"""Regresiones de la auditoría integral: despliegue, cabeceras y robustez."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import app

COMPOSE = Path(__file__).resolve().parents[1] / "compose.yaml"


def _servicios() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


@pytest.mark.no_database
def test_compose_todos_los_servicios_rotan_logs():
    for nombre, servicio in _servicios().items():
        opciones = servicio.get("logging", {}).get("options", {})
        assert opciones.get("max-size") and opciones.get("max-file"), nombre


@pytest.mark.no_database
def test_compose_workers_tienen_healthcheck_y_gracia_de_apagado():
    workers = {n: s for n, s in _servicios().items() if "worker" in n}
    assert len(workers) == 12
    for nombre, servicio in workers.items():
        assert servicio["healthcheck"]["test"][-2:] == ["-m", "worker.healthcheck"], nombre
        # Debe superar los 10 s por defecto de Docker para terminar la operación en curso.
        assert int(str(servicio["stop_grace_period"]).rstrip("s")) >= 60, nombre


def test_cabeceras_defensivas_en_respuestas_publicas():
    with TestClient(app) as client:
        respuesta = client.get("/health/live")
    assert respuesta.headers["x-frame-options"] == "DENY"
    assert respuesta.headers["referrer-policy"] == "no-referrer"
    assert respuesta.headers["x-content-type-options"] == "nosniff"


@pytest.mark.no_database
def test_entorno_mal_formado_no_tumba_la_api(monkeypatch):
    from api.main import _worker_stale_seconds

    monkeypatch.setenv("IACP_WORKER_STALE_SECONDS", "no-es-un-numero")
    assert _worker_stale_seconds() == 900.0


def test_retencion_mal_escrita_no_borra_historial(monkeypatch):
    from api.db import prune_history

    monkeypatch.setenv("IACP_RETENTION_DAYS", "treinta")
    assert prune_history() == {"events": 0, "operations": 0, "idempotency_keys": 0}


@pytest.mark.no_database
def test_clave_maestra_se_cachea_y_se_recarga_al_rotar(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from api import service_secrets

    ruta = tmp_path / "key"
    primera = Fernet.generate_key()
    ruta.write_bytes(primera + b"\n")
    monkeypatch.setenv("IACP_SERVICE_KEY_FILE", str(ruta))
    cifrado = service_secrets.encrypt({"a": 1})
    assert service_secrets.cipher() is service_secrets.cipher()
    assert service_secrets.decrypt(cifrado) == {"a": 1}

    ruta.write_bytes(Fernet.generate_key() + b"\n")  # rotación: cambia el tamaño/mtime
    import os

    os.utime(ruta, ns=(1, 2_000_000_000_000_000_000))
    with pytest.raises(Exception):
        service_secrets.decrypt(cifrado)
