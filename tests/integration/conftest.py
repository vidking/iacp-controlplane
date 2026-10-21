import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from iacp import db


@pytest.fixture
def pg_engine():
    dsn = os.environ.get("IACP_INTEGRATION_DATABASE_URL")
    if not dsn:
        pytest.skip("Set IACP_INTEGRATION_DATABASE_URL to a disposable PostgreSQL 17 database")
    url = make_url(dsn)
    if url.host not in {"127.0.0.1", "localhost", "::1", "test-db"} or not (url.database or "").startswith("iacp_test_"):
        pytest.fail("Integration requires an explicitly named local/Compose iacp_test_ database")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    name = "iacp_test_" + uuid4().hex
    with admin.connect() as connection:
        assert connection.exec_driver_sql("SHOW server_version").scalar().startswith("17.")
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine = create_engine(url.set(database=name), pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}"')
        admin.dispose()


@pytest.fixture
def runtime(pg_engine, auth, tmp_path, monkeypatch):
    redis = os.environ.get("IACP_INTEGRATION_REDIS_URL")
    if not redis:
        pytest.skip("Set IACP_INTEGRATION_REDIS_URL to disposable Redis 7 database 15")
    redis_url = make_url(redis)
    if redis_url.host not in {"127.0.0.1", "localhost", "::1", "test-redis"} or redis_url.database != "15":
        pytest.fail("Integration requires local/Compose Redis database 15")
    url = pg_engine.url
    password = tmp_path / "db_password"
    password.write_text(url.password or "")
    for key, value in {
        "POSTGRES_DB": url.database, "POSTGRES_USER": url.username,
        "DATABASE_HOST": url.host, "DATABASE_PORT": str(url.port or 5432),
        "DATABASE_PASSWORD_FILE": str(password), "CELERY_BROKER_URL": redis,
    }.items():
        monkeypatch.setenv(key, value)
    db.get_session_factory.cache_clear()
    db.get_engine.cache_clear()
    try:
        yield auth
    finally:
        db.get_session_factory.cache_clear()
        db.get_engine().dispose()
        db.get_engine.cache_clear()
