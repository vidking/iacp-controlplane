"""Fixtures compartidos para la línea PostgreSQL del Control Plane."""

import os
from urllib.parse import unquote, urlsplit

import pytest


def _test_database_url() -> str:
    return (os.getenv("IACP_TEST_DATABASE_URL") or "").strip()


def pytest_configure(config):
    value = _test_database_url()
    if value:
        database_name = unquote(urlsplit(value).path.lstrip("/")).lower()
        if not (database_name.endswith("_test") or database_name.endswith("-test") or database_name == "test"):
            raise pytest.UsageError(
                "IACP_TEST_DATABASE_URL debe apuntar a una base cuyo nombre termine en _test o -test"
            )
        os.environ["IACP_DATABASE_URL"] = value


def pytest_collection_modifyitems(config, items):
    if _test_database_url():
        return
    skip = pytest.mark.skip(reason="requiere IACP_TEST_DATABASE_URL apuntando a PostgreSQL")
    for item in items:
        if item.fspath.basename in {"test_api.py", "test_worker.py"}:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def clean_postgresql_state(request, monkeypatch):
    monkeypatch.setenv("IACP_REQUIRE_API_TOKEN", "0")
    monkeypatch.delenv("IACP_API_TOKEN", raising=False)
    if not _test_database_url():
        yield
        return

    from api.db import database, initialize

    initialize()
    with database() as db:
        db.execute(
            """
            TRUNCATE TABLE
              events, idempotency_keys, ip_allocations, vmid_leases,
              instances, operations, workers, schema_meta
            RESTART IDENTITY CASCADE
            """
        )
    initialize()
    yield
