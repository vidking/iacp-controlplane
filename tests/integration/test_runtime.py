from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from alembic import command
from fastapi.testclient import TestClient
import pytest
from redis import Redis
from sqlalchemy import inspect, select

from iacp.main import create_app
from iacp.migrations import alembic_config, schema_is_current
from iacp.models import Job
from tests.helpers import create_legacy_jobs, upgrade

pytestmark = pytest.mark.integration


def test_fresh_postgres_schema_matches_models(pg_engine):
    upgrade(pg_engine)
    upgrade(pg_engine)
    with pg_engine.begin() as connection:
        assert schema_is_current(connection)
        config = alembic_config()
        config.attributes["connection"] = connection
        command.check(config)


def test_postgres_adopts_legacy_jobs_without_changes(pg_engine):
    create_legacy_jobs(pg_engine)
    with pg_engine.begin() as connection:
        connection.execute(Job.__table__.insert(), {
            "id": str(uuid4()), "operation": "smoke", "status": "completed",
            "message": "bootstrap preserved", "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })
        before = connection.execute(select(Job.__table__)).all()
    upgrade(pg_engine)
    upgrade(pg_engine)
    with pg_engine.connect() as connection:
        assert connection.execute(select(Job.__table__)).all() == before
        assert schema_is_current(connection)


def test_postgres_schema_drift_rolls_back_migration(pg_engine):
    create_legacy_jobs(pg_engine)
    with pg_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE jobs ALTER COLUMN operation TYPE VARCHAR(99)")
    with pytest.raises(RuntimeError, match="manual review required"):
        upgrade(pg_engine)
    assert set(inspect(pg_engine).get_table_names()) == {"jobs"}


def test_api_redis_real_celery_postgres_and_repeated_delivery(pg_engine, runtime, tmp_path):
    from iacp.worker import celery_app

    upgrade(pg_engine)
    broker = os.environ["CELERY_BROKER_URL"]
    celery_app.conf.broker_url = broker
    with Redis.from_url(broker) as redis:
        assert redis.info("server")["redis_version"].startswith("7.")
    child_env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "app")}
    with (tmp_path / "worker.log").open("w+") as log:
        worker = subprocess.Popen([
            sys.executable, "-m", "celery", "-A", "iacp.worker:celery_app", "worker",
            "--pool=solo", "--concurrency=1", "--loglevel=WARNING",
            "--without-gossip", "--without-mingle", "--without-heartbeat",
        ], env=child_env, stdout=log, stderr=subprocess.STDOUT)
        try:
            with TestClient(create_app()) as client:
                assert client.get("/health/ready").status_code == 200
                accepted = client.post("/v1/jobs/smoke", headers=runtime)
                assert accepted.status_code == 202
                job_id = accepted.json()["job_id"]
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    response = client.get(f"/v1/jobs/{job_id}", headers=runtime)
                    assert response.status_code == 200
                    if response.json()["status"] == "completed":
                        break
                    assert worker.poll() is None, "Worker exited before completing the job"
                    time.sleep(0.1)
                else:
                    pytest.fail("Real worker did not complete the smoke job")
                assert "Terraform NO fue ejecutado" in response.json()["message"]
                finished = response.json()
                # The task handler is also exercised against an actual terminal
                # row. Concurrent delivery/recovery is explicitly a later gate.
                from iacp.worker import smoke
                smoke.run(job_id)
                assert client.get(f"/v1/jobs/{job_id}", headers=runtime).json() == finished
            with TestClient(create_app()) as restarted:
                assert restarted.get(f"/v1/jobs/{job_id}", headers=runtime).json() == finished
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
            celery_app.close()


def test_postgres_ready_requires_migration(pg_engine, runtime):
    with TestClient(create_app()) as client:
        assert client.get("/health/ready").status_code == 503


def test_migration_cli_uses_runtime_configuration(pg_engine, runtime):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "app/alembic.ini", "upgrade", "head"],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root / "app")},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    with pg_engine.connect() as connection:
        assert schema_is_current(connection)
