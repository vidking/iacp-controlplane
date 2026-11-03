from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_postgres_schema(monkeypatch, tmp_path):
    base_conninfo = os.getenv("IACP_TEST_DATABASE_URL", "").strip()
    if not base_conninfo:
        pytest.skip("Define IACP_TEST_DATABASE_URL para ejecutar pruebas PostgreSQL aisladas")

    database_name = conninfo_to_dict(base_conninfo).get("dbname", "")
    if not re.search(r"(^|[_-])test([_-]|$)", database_name, re.IGNORECASE):
        pytest.fail("IACP_TEST_DATABASE_URL debe apuntar a una base cuyo nombre incluya _test")

    schema_name = f"pytest_{uuid.uuid4().hex}"
    with psycopg.connect(base_conninfo, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))

    isolated_conninfo = make_conninfo(
        base_conninfo,
        options=f"-c search_path={schema_name},public",
    )
    monkeypatch.setenv("IACP_DATABASE_URL", isolated_conninfo)
    monkeypatch.setenv("IACP_RUNTIME_STAGE", "develop")
    monkeypatch.setenv("IACP_WASABI_ENABLED", "false")
    monkeypatch.setenv("IACP_DEFAULT_ENVIRONMENT", "lab")
    monkeypatch.setenv("IACP_ENABLED_ENVIRONMENTS", "lab")
    monkeypatch.setenv("IACP_TERRAFORM_PATH", str(REPO_ROOT / "terraform"))
    monkeypatch.setenv("IACP_TERRAFORM_MNG_DIR", "environments/mng01")
    monkeypatch.setenv("IACP_MNG_CATALOG", str(REPO_ROOT / "terraform/environments/mng01/catalog.json"))
    monkeypatch.setenv("IACP_PROXMOX_MNG_ENDPOINT", "https://mng01.test.invalid:8006/")
    monkeypatch.setenv("IACP_PROXMOX_MNG_API_TOKEN", "test-user@pam!test-token=not-a-secret")
    monkeypatch.setenv("IACP_PROXMOX_MNG_POOL_ID", "iacp-lab")
    monkeypatch.setenv("IACP_WORKDATA_DIR", str(tmp_path / "workdata"))
    monkeypatch.setenv("IACP_ROOT_PASSWORD_MIN_LENGTH", "12")
    monkeypatch.setenv("IACP_API_TOKEN", "")
    monkeypatch.setenv("IACP_REQUIRE_API_TOKEN", "0")
    monkeypatch.setenv("IACP_MAX_CONCURRENT", "2")
    monkeypatch.setenv("IACP_MAX_QUEUE_DEPTH", "0")
    monkeypatch.delenv("IACP_PRX_ENABLED", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("IACP_WASABI_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("IACP_WASABI_SECRET_ACCESS_KEY", raising=False)

    try:
        yield
    finally:
        with psycopg.connect(base_conninfo, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))
