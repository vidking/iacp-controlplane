from datetime import datetime, timezone
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import inspect, select, text

from iacp.migrations import alembic_config, schema_is_current
from iacp.models import Job
from tests.helpers import create_legacy_jobs, legacy_snapshot, upgrade


def test_fresh_schema_matches_models_and_repeated_upgrade_preserves_it(empty_engine):
    upgrade(empty_engine)
    upgrade(empty_engine)
    assert set(inspect(empty_engine).get_table_names()) == {"jobs", "alembic_version", "tenants", "instances", "operations", "operation_events", "terraform_executions", "terraform_attempts", "terraform_applies", "power_executions", "power_ownership_certificates", "deletion_executions", "network_discoveries", "lab_policies", "guest_initializations"}
    with empty_engine.begin() as connection:
        assert schema_is_current(connection)
        config = alembic_config()
        config.attributes["connection"] = connection
        command.check(config)


def test_adopts_bootstrap_without_altering_rows(empty_engine):
    create_legacy_jobs(empty_engine)
    now = datetime.now(timezone.utc)
    with empty_engine.begin() as connection:
        connection.execute(Job.__table__.insert(), {
            "id": str(uuid4()), "operation": "smoke", "status": "completed",
            "message": "legacy job", "created_at": now, "updated_at": now,
        })
        before = legacy_snapshot(connection)
    upgrade(empty_engine)
    with empty_engine.connect() as connection:
        assert legacy_snapshot(connection) == before
        assert connection.execute(text("SELECT tenant_id, operation_id FROM jobs")).one() == (None, None)
        assert schema_is_current(connection)


@pytest.mark.parametrize("drift", ["column", "index", "nullable", "type", "pk", "check"])
def test_refuses_unknown_legacy_schema(empty_engine, drift):
    create_legacy_jobs(empty_engine)
    with empty_engine.begin() as connection:
        if drift == "column":
            connection.exec_driver_sql("ALTER TABLE jobs ADD COLUMN unexpected TEXT")
        elif drift == "index":
            connection.exec_driver_sql("DROP INDEX ix_jobs_status")
        else:
            connection.exec_driver_sql("DROP TABLE jobs")
            columns = {
                "nullable": "id VARCHAR(36) NOT NULL PRIMARY KEY, operation VARCHAR(30)",
                "type": "id VARCHAR(36) NOT NULL PRIMARY KEY, operation VARCHAR(99) NOT NULL",
                "pk": "id VARCHAR(36) NOT NULL, operation VARCHAR(30) NOT NULL PRIMARY KEY",
                "check": "id VARCHAR(36) NOT NULL PRIMARY KEY, operation VARCHAR(30) NOT NULL CHECK (operation = 'smoke')",
            }
            connection.exec_driver_sql(f"""CREATE TABLE jobs (
                {columns[drift]}, status VARCHAR(30) NOT NULL, message TEXT NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            )""")
            connection.exec_driver_sql("CREATE INDEX ix_jobs_status ON jobs(status)")
    with pytest.raises(RuntimeError, match="manual review required"):
        upgrade(empty_engine)
    assert "jobs" in inspect(empty_engine).get_table_names()
    with empty_engine.connect() as connection:
        assert not schema_is_current(connection)


def test_downgrade_cannot_drop_persistent_jobs(engine):
    with engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="conservar autorizaciones"):
            command.downgrade(config, "base")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0010_guest_identity"
    assert "jobs" in inspect(engine).get_table_names()
