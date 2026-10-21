import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from iacp.api import health
from iacp.api.dependencies import get_smoke_publisher
from iacp.main import create_app
from iacp.models import Job


def test_import_startup_liveness_and_contract_need_no_secrets(monkeypatch):
    monkeypatch.delenv("DATABASE_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("IACP_API_TOKEN_FILE", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/openapi.json").status_code == 200


@pytest.mark.parametrize("route,method", [
    ("/v1/jobs/smoke", "post"), ("/v1/jobs/missing", "get"), ("/v1/instances", "post"),
])
@pytest.mark.parametrize("header", [None, "invalid", "Basic invalid", "Bearer invalid", "Bearer"])
def test_all_protected_routes_reject_bad_auth(client, auth, sessions, route, method, header):
    response = getattr(client, method)(route, headers={"Authorization": header} if header else {})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_bare_token_is_rejected(client, auth):
    bare = auth["Authorization"].removeprefix("Bearer ")
    assert client.post("/v1/jobs/smoke", headers={"Authorization": bare}).status_code == 401


@pytest.mark.parametrize("configuration", ["missing", "unreadable", "short"])
def test_missing_auth_configuration_fails_closed(client, auth, tmp_path, monkeypatch, configuration):
    if configuration == "missing":
        monkeypatch.delenv("IACP_API_TOKEN_FILE")
    elif configuration == "unreadable":
        monkeypatch.setenv("IACP_API_TOKEN_FILE", str(tmp_path / "not-present"))
    else:
        Path(os.environ["IACP_API_TOKEN_FILE"]).write_text("short")
    response = client.post("/v1/jobs/smoke", headers=auth)
    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication unavailable"}


def test_smoke_commits_before_publishing_and_returns_existing_contract(client, application, sessions, auth):
    published = []

    def publish(job_id):
        with sessions() as session:
            assert session.get(Job, job_id).status == "queued"
        published.append(job_id)

    application.dependency_overrides[get_smoke_publisher] = lambda: publish
    response = client.post("/v1/jobs/smoke", headers=auth)
    assert response.status_code == 202
    assert response.json() == {"job_id": published[0], "status": "queued"}
    job = client.get(f"/v1/jobs/{published[0]}", headers=auth)
    assert job.status_code == 200
    assert set(job.json()) == {"job_id", "operation", "status", "message", "created_at", "updated_at"}
    assert job.json()["operation"] == "smoke"
    assert job.json()["created_at"]


def test_queue_failure_is_persisted_without_leaking_exception(client, application, sessions, auth):
    def fail(job_id):
        raise ConnectionError("sensitive connection information")

    application.dependency_overrides[get_smoke_publisher] = lambda: fail
    response = client.post("/v1/jobs/smoke", headers=auth)
    assert response.status_code == 503
    assert response.json() == {"detail": "Queue unavailable"}
    with sessions() as session:
        job = session.scalars(select(Job)).one()
        assert job.status == "failed"
        assert "sensitive" not in job.message


def test_ambiguous_publish_does_not_overwrite_completed_job(client, application, sessions, auth):
    def complete_then_fail(job_id):
        with sessions() as session:
            job = session.get(Job, job_id)
            job.status = "completed"
            job.message = "completed by worker"
            session.commit()
        raise TimeoutError("ambiguous acknowledgement")

    application.dependency_overrides[get_smoke_publisher] = lambda: complete_then_fail
    assert client.post("/v1/jobs/smoke", headers=auth).status_code == 503
    with sessions() as session:
        job = session.scalars(select(Job)).one()
        assert job.status == "completed"
        assert job.message == "completed by worker"


def test_missing_job_remains_404(client, auth):
    assert client.get("/v1/jobs/not-a-uuid", headers=auth).json() == {"detail": "Job not found"}
    assert client.get("/v1/jobs/not-a-uuid", headers=auth).status_code == 404


def test_provisioning_stays_disabled(client, auth):
    response = client.post("/v1/instances", headers=auth)
    assert response.status_code == 503
    assert response.json()["code"] == "IAC_EXECUTOR_NOT_CONFIGURED"


def test_readiness_checks_versioned_schema_and_redis(client, engine, monkeypatch):
    redis = MagicMock()
    monkeypatch.setattr(health, "get_engine", lambda: engine)
    monkeypatch.setattr(health.Redis, "from_url", redis)
    assert client.get("/health/ready").json() == {"status": "ready"}
    redis.return_value.__enter__.return_value.ping.assert_called_once()
    assert redis.call_args.kwargs["socket_timeout"] == 3


def test_readiness_rejects_unmigrated_db(client, empty_engine, monkeypatch):
    # client has its own migrated fixture; explicitly remove only the revision.
    with empty_engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM alembic_version")
    redis = MagicMock()
    monkeypatch.setattr(health, "get_engine", lambda: empty_engine)
    monkeypatch.setattr(health.Redis, "from_url", redis)
    assert client.get("/health/ready").status_code == 503
    redis.assert_not_called()


def test_readiness_hides_dependency_failures(client, engine, monkeypatch):
    monkeypatch.setattr(health, "get_engine", lambda: engine)
    monkeypatch.setattr(health.Redis, "from_url", MagicMock(side_effect=OSError("sensitive info")))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert "sensitive" not in response.text


def test_openapi_snapshot_and_security():
    schema = create_app().openapi()
    expected = json.loads((Path(__file__).resolve().parents[1] / "docs/openapi.json").read_text())
    assert schema == expected, "Regenerate docs/openapi.json after reviewing contract changes"
    ids = []
    for path, operations in schema["paths"].items():
        for operation in operations.values():
            ids.append(operation["operationId"])
            if path.startswith("/v1/"):
                assert operation["security"] == [{"HTTPBearer": []}]
                assert "401" in operation["responses"]
    assert len(ids) == len(set(ids))
    assert "job_id" in schema["components"]["schemas"]["JobResponse"]["properties"]
