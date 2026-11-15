"""La contraseña root y los tokens no aparecen en respuestas, base de datos ni eventos."""

import json

from fastapi.testclient import TestClient

from api.db import database
from api.main import app
from test_api import public_body

ROOT_PASSWORD = "Inicial-Segura-123!"


def test_root_password_never_leaves_the_secret_file():
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "secret-exposure"},
            json=public_body("secret-exposure", password_only=True),
        )
        assert created.status_code == 202
        operation_id = created.json()["operation_id"]
        instance_id = created.json()["instance_id"]
        replay = client.post(
            "/v1/instances",
            headers={"Idempotency-Key": "secret-exposure"},
            json=public_body("secret-exposure", password_only=True),
        )
        responses = [
            created.text,
            replay.text,
            client.get(f"/v1/operations/{operation_id}").text,
            client.get(f"/v1/instances/{instance_id}").text,
            client.get("/v1/instances", params={"status": "all"}).text,
            client.get("/v1/operations").text,
            client.get("/v1/observability").text,
        ]
    for body in responses:
        assert ROOT_PASSWORD not in body

    with database() as db:
        dumps = [
            json.dumps([dict(row) for row in db.execute(f"SELECT * FROM {table}").fetchall()], default=str)
            for table in ("operations", "instances", "events", "idempotency_keys", "workers")
        ]
        secret_path = db.execute(
            "SELECT secret_path FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()["secret_path"]
    for dump in dumps:
        assert ROOT_PASSWORD not in dump
    # El secreto vive solo en el archivo de un uso, con permisos restringidos.
    with open(secret_path, encoding="utf-8") as handle:
        assert handle.read().strip() == ROOT_PASSWORD


def test_api_token_is_not_echoed_in_errors(monkeypatch):
    monkeypatch.setenv("IACP_API_TOKEN", "token-que-no-debe-filtrarse-123")
    monkeypatch.setenv("IACP_REQUIRE_API_TOKEN", "1")
    with TestClient(app) as client:
        wrong = client.get("/v1/instances", headers={"Authorization": "Bearer otro-token-cualquiera"})
    assert wrong.status_code == 401
    assert "token-que-no-debe-filtrarse-123" not in wrong.text
