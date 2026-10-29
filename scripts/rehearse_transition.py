"""Ensaya la adopción del bootstrap únicamente en el Compose de desarrollo."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from redis import Redis
from sqlalchemy import inspect, text

from iacp.db import get_engine
from iacp.migrations import schema_is_current

KEY = "iacp:rehearsal:metadata"


def guard():
    broker = urlsplit(os.environ.get("CELERY_BROKER_URL", ""))
    if not (
        os.environ.get("IACP_REHEARSAL") == "1"
        and os.environ.get("DATABASE_HOST") == "dev-db"
        and os.environ.get("POSTGRES_DB", "").startswith("iacp_dev")
        and broker.hostname == "dev-redis"
        and broker.path == "/0"
    ):
        raise RuntimeError("El ensayo requiere un proyecto de desarrollo nuevo y autorización explícita")


def fingerprint():
    return {
        key: hashlib.sha256(Path(os.environ[key]).read_bytes()).hexdigest()
        for key in ("DATABASE_PASSWORD_FILE", "IACP_API_TOKEN_FILE")
    }


def row_snapshot(connection, job_id):
    row = connection.execute(text("SELECT id, operation, status, message, created_at, updated_at FROM jobs WHERE id = :id"), {"id": job_id}).mappings().one()
    return {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}


def seed(engine, redis):
    if inspect(engine).has_table("jobs") or redis.exists(KEY):
        raise RuntimeError("El ensayo solo puede inicializar un entorno nuevo; no se reemplazaron datos")
    job_id = str(uuid4())
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE jobs (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                operation VARCHAR(30) NOT NULL, status VARCHAR(30) NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """)
        connection.exec_driver_sql("CREATE INDEX ix_jobs_status ON jobs(status)")
        connection.execute(text("""
            INSERT INTO jobs (id, operation, status, message, created_at, updated_at)
            VALUES (:id, 'smoke', 'completed', 'Job sintético del bootstrap', now(), now())
        """), {"id": job_id})
        snapshot = row_snapshot(connection, job_id)
    metadata = {"job_id": job_id, "row": snapshot, "secrets": fingerprint()}
    if not redis.set(KEY, json.dumps(metadata), nx=True):
        raise RuntimeError("No se pudo registrar el marcador del ensayo")
    return {"status": "seeded", "job_id": job_id}


def verify(engine, redis):
    raw = redis.get(KEY)
    if raw is None:
        raise RuntimeError("No se conservó el marcador Redis del ensayo")
    metadata = json.loads(raw)
    if metadata["secrets"] != fingerprint():
        raise RuntimeError("Cambió un secreto de desarrollo durante el ensayo")
    with engine.connect() as connection:
        if not schema_is_current(connection):
            raise RuntimeError("La base no alcanzó la revisión Alembic esperada")
        if row_snapshot(connection, metadata["job_id"]) != metadata["row"]:
            raise RuntimeError("Cambió el job del bootstrap durante el ensayo")
        inventory = [dict(row) for row in connection.execute(text("""
            SELECT i.id, i.status AS instance_status, o.id AS operation_id,
                   o.status AS operation_status, j.id AS job_id, j.status AS job_status
            FROM instances i JOIN operations o ON o.instance_id = i.id
            JOIN jobs j ON j.operation_id = o.id ORDER BY i.id
        """)).mappings()]
        if any(row["instance_status"] != "ready" or row["operation_status"] != "completed"
               or row["job_status"] != "completed" for row in inventory):
            raise RuntimeError("El inventario de desarrollo contiene estados no terminales inconsistentes")
        if "inventory" in metadata and metadata["inventory"] != inventory:
            raise RuntimeError("Cambió el inventario simulado durante la recreación")
    if "inventory" not in metadata:
        metadata["inventory"] = inventory
        redis.set(KEY, json.dumps(metadata))
    return {"status": "passed", "bootstrap_job_preserved": True, "redis_marker_preserved": True,
            "secrets_preserved": True, "inventory_preserved": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("seed", "verify"))
    args = parser.parse_args()
    guard()
    engine = get_engine()
    try:
        with Redis.from_url(os.environ["CELERY_BROKER_URL"], socket_timeout=5) as redis:
            result = seed(engine, redis) if args.operation == "seed" else verify(engine, redis)
            print(json.dumps(result))
    finally:
        engine.dispose()
