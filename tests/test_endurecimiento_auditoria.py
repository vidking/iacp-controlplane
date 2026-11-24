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


TOKEN = "t" * 40


def _con_token(monkeypatch, maximo="3"):
    monkeypatch.setenv("IACP_API_TOKEN", TOKEN)
    monkeypatch.setenv("IACP_ALLOW_INSECURE_LOCAL_DEV", "0")
    monkeypatch.setenv("IACP_AUTH_MAX_FAILURES", maximo)
    monkeypatch.setenv("IACP_AUTH_FAILURE_WINDOW_SECONDS", "60")


def test_limitador_bloquea_tokens_invalidos_pero_nunca_al_valido(monkeypatch):
    _con_token(monkeypatch)
    invalido = {"Authorization": "Bearer " + "x" * 40}
    valido = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/v1/operations", headers=invalido).status_code == 401
        bloqueado = client.get("/v1/operations", headers=invalido)
        assert bloqueado.status_code == 429
        assert int(bloqueado.headers["retry-after"]) >= 1
        # También se frena sin credenciales y en métodos con cuerpo.
        assert client.post("/v1/operations", json={}).status_code == 429
        # El operador legítimo no se ve afectado por el bloqueo del origen.
        assert client.get("/v1/operations", headers=valido).status_code == 200


def test_limitador_expira_con_la_ventana(monkeypatch):
    from api.main import AUTH_THROTTLE

    _con_token(monkeypatch, maximo="2")
    monkeypatch.setenv("IACP_AUTH_FAILURE_WINDOW_SECONDS", "1")
    AUTH_THROTTLE.record("origen")
    AUTH_THROTTLE.record("origen")
    assert AUTH_THROTTLE.retry_after("origen") >= 1
    import time

    time.sleep(1.1)
    assert AUTH_THROTTLE.retry_after("origen") == 0


@pytest.mark.no_database
def test_limitador_acota_la_memoria_por_origen():
    from api.main import AuthFailureThrottle

    limitador = AuthFailureThrottle()
    for indice in range(AuthFailureThrottle.MAX_SOURCES + 50):
        limitador.record(f"10.0.{indice // 256}.{indice % 256}")
    assert len(limitador._failures) <= AuthFailureThrottle.MAX_SOURCES


def test_token_corto_impide_el_arranque(monkeypatch):
    monkeypatch.setenv("IACP_API_TOKEN", "corto-pero-no-vacio")
    monkeypatch.setenv("IACP_API_TOKEN_MIN_LENGTH", "32")
    monkeypatch.setenv("IACP_ALLOW_INSECURE_LOCAL_DEV", "0")
    with pytest.raises(RuntimeError, match="al menos 32 caracteres"):
        with TestClient(app):
            pass


def test_token_largo_arranca(monkeypatch):
    _con_token(monkeypatch)
    monkeypatch.setenv("IACP_API_TOKEN_MIN_LENGTH", "32")
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200


PORTAINER = COMPOSE.parent / "deploy" / "portainer" / "docker-compose.yml"
DOCKERFILE = COMPOSE.parent / "Dockerfile"


def _stack(ruta: Path) -> dict:
    return yaml.safe_load(ruta.read_text(encoding="utf-8"))["services"]


@pytest.mark.no_database
@pytest.mark.parametrize("ruta", [COMPOSE, PORTAINER], ids=["compose", "portainer"])
def test_contenedores_tienen_techos_de_memoria_y_procesos(ruta):
    servicios = _stack(ruta)
    assert {"api", "postgres", "worker-1", "service-worker-1"} <= set(servicios)
    for nombre, servicio in servicios.items():
        assert servicio.get("mem_limit"), f"{nombre} sin mem_limit"
        assert servicio.get("pids_limit"), f"{nombre} sin pids_limit"


@pytest.mark.no_database
@pytest.mark.parametrize("ruta", [COMPOSE, PORTAINER], ids=["compose", "portainer"])
def test_postgres_endurecido_con_capacidades_minimas(ruta):
    postgres = _stack(ruta)["postgres"]
    assert postgres["read_only"] is True
    assert "no-new-privileges:true" in postgres["security_opt"]
    assert postgres["cap_drop"] == ["ALL"]
    assert set(postgres["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}
    assert not postgres.get("ports"), "PostgreSQL nunca se publica en el host"


@pytest.mark.no_database
def test_portainer_conserva_aislamiento_de_red_de_postgres():
    contenido = yaml.safe_load(PORTAINER.read_text(encoding="utf-8"))
    assert contenido["networks"]["internal"]["internal"] is True
    assert contenido["services"]["postgres"]["networks"] == ["internal"]


@pytest.mark.no_database
def test_dockerfile_deja_el_codigo_de_solo_lectura_para_el_usuario_de_runtime():
    texto = DOCKERFILE.read_text(encoding="utf-8")
    assert "chown -R iacp:iacp /app" not in texto
    assert "chown -R iacp:iacp /var/lib/iacp-controlplane" in texto
    assert "chmod -R go-w,a+rX /app" in texto
    assert "\nUSER iacp\n" in texto


def _entorno_firmador(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    clave = Fernet.generate_key()
    ruta = tmp_path / "service-key"
    ruta.write_bytes(clave + b"\n")
    _con_token(monkeypatch)
    monkeypatch.setenv("IACP_SERVICE_KEY_FILE", str(ruta))
    monkeypatch.setenv("IACP_SERVICES_ENABLED", "1")
    monkeypatch.setenv("IACP_SERVICE_IP_TLS_ENABLED", "1")
    return clave.decode()


def test_firmador_tls_es_alcanzable_por_workers_con_el_token_derivado(monkeypatch, tmp_path):
    """Regresión: la guardia exigía el token de la API y el endpoint devolvía 401."""
    from api.service_secrets import signer_token

    _entorno_firmador(monkeypatch, tmp_path)
    ruta = "/v1/internal/service-jobs/job-inexistente/tls-certificate"
    with TestClient(app) as client:
        respuesta = client.post(ruta, headers={"Authorization": f"Bearer {signer_token()}"})
    # Llega al endpoint: la autenticación pasó y falla después por el job inexistente.
    assert respuesta.status_code == 404
    assert respuesta.json()["detail"] == "SERVICE_JOB_NOT_FOUND"


def test_firmador_tls_rechaza_clave_maestra_token_de_api_y_ausencia(monkeypatch, tmp_path):
    clave = _entorno_firmador(monkeypatch, tmp_path)
    monkeypatch.setenv("IACP_AUTH_MAX_FAILURES", "50")  # aquí se prueba la autenticación, no el limitador
    ruta = "/v1/internal/service-jobs/job/tls-certificate"
    with TestClient(app) as client:
        for cabeceras in (
            {"Authorization": f"Bearer {clave}"},  # la clave maestra ya no es una credencial
            {"Authorization": f"Bearer {TOKEN}"},  # el token de la API tampoco
            {"Authorization": "Bearer ñandú".encode("utf-8")},  # no ASCII: antes provocaba TypeError/500
            {},
        ):
            respuesta = client.post(ruta, headers=cabeceras)
            assert respuesta.status_code == 401, cabeceras
            assert respuesta.json()["detail"] == "SERVICE_SIGNER_UNAUTHORIZED"


def test_firmador_tls_limita_intentos_pero_no_bloquea_al_worker_legitimo(monkeypatch, tmp_path):
    from api.service_secrets import signer_token

    _entorno_firmador(monkeypatch, tmp_path)
    ruta = "/v1/internal/service-jobs/job/tls-certificate"
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post(ruta, headers={"Authorization": "Bearer mal"}).status_code == 401
        assert client.post(ruta, headers={"Authorization": "Bearer mal"}).status_code == 429
        legitimo = client.post(ruta, headers={"Authorization": f"Bearer {signer_token()}"})
        assert legitimo.status_code == 404  # autenticado; el job no existe


@pytest.mark.no_database
def test_token_derivado_no_revela_la_clave_y_cambia_con_ella(tmp_path):
    from api.service_secrets import signer_token

    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"clave-uno\n")
    b.write_bytes(b"clave-dos\n")
    token = signer_token(str(a))
    assert token == signer_token(str(a)) != signer_token(str(b))
    assert "clave-uno" not in token and len(token) == 64
