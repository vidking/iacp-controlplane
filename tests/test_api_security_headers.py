"""Cabeceras de seguridad y límite de cuerpo de la API."""

import pytest
from fastapi.testclient import TestClient

from api.main import app, startup


def test_respuestas_operativas_no_se_almacenan_y_no_cargan_recursos():
    with TestClient(app) as client:
        for path in ("/health/ready", "/v1/plans"):
            response = client.get(path)
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
            assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_portal_html_restringe_recursos_a_su_origen():
    with TestClient(app) as client:
        response = client.get("/docs")
        assert response.status_code == 200
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_referencias_interactivas_fijan_integridad_de_scripts():
    with TestClient(app) as client:
        for path in ("/swagger", "/redoc"):
            response = client.get(path)
            assert response.status_code == 200
            assert 'integrity="sha384-' in response.text
            assert "script-src 'sha384-" in response.headers["Content-Security-Policy"]
            assert "connect-src 'self'" in response.headers["Content-Security-Policy"]


def test_cuerpo_excesivo_se_rechaza_antes_de_procesarlo(monkeypatch):
    monkeypatch.setenv("IACP_MAX_REQUEST_BODY_BYTES", "2048")
    with TestClient(app) as client:
        response = client.post(
            "/v1/instances",
            content=b"{" + b" " * 4096 + b"}",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413


def test_content_length_invalido_se_rechaza():
    with TestClient(app) as client:
        response = client.post(
            "/v1/instances",
            content=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "abc"},
        )
        assert response.status_code == 400


def test_limite_acumulado_para_cuerpo_sin_longitud(monkeypatch):
    monkeypatch.setenv("IACP_MAX_REQUEST_BODY_BYTES", "1024")
    with TestClient(app) as client:
        response = client.post("/v1/instances", content=iter((b" " * 700, b" " * 700)))
        assert response.status_code == 413
        assert response.headers["Cache-Control"] == "no-store"


def test_error_de_modelo_conserva_esquema_sin_reflejar_entrada():
    with TestClient(app) as client:
        response = client.post("/v1/service-instances", json={"application_id": "valor-de-prueba"})
        assert response.status_code == 422
        assert response.json()["detail"]
        assert all(set(error) == {"loc", "type", "msg"} for error in response.json()["detail"])
        assert "valor-de-prueba" not in response.text


@pytest.mark.no_database
def test_inicio_exige_token_antes_de_inicializar_postgres(monkeypatch):
    monkeypatch.delenv("IACP_API_TOKEN", raising=False)
    monkeypatch.delenv("IACP_ALLOW_INSECURE_LOCAL_DEV", raising=False)
    with pytest.raises(RuntimeError, match="IACP_API_TOKEN"):
        startup()
