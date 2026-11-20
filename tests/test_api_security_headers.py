"""Cabeceras de seguridad y límite de cuerpo de la API."""

from fastapi.testclient import TestClient

from api.main import app


def test_respuestas_operativas_no_se_almacenan_y_no_cargan_recursos():
    with TestClient(app) as client:
        for path in ("/health/ready", "/v1/plans"):
            response = client.get(path)
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
            assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_portal_html_no_recibe_la_csp_restrictiva_de_json():
    with TestClient(app) as client:
        response = client.get("/docs")
        assert response.status_code == 200
        assert "Content-Security-Policy" not in response.headers


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
