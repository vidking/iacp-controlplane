from alembic import command
from sqlalchemy import text

from iacp.migrations import alembic_config


def upgrade(engine, revision="head"):
    with engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def legacy_snapshot(connection):
    return connection.execute(text("SELECT id, operation, status, message, created_at, updated_at FROM jobs ORDER BY id")).all()


def create_legacy_jobs(engine):
    # Independent copy of the DDL produced by bootstrap a964850, not the ORM.
    timestamp = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "DATETIME"
    with engine.begin() as connection:
        connection.exec_driver_sql(f"""
            CREATE TABLE jobs (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                operation VARCHAR(30) NOT NULL,
                status VARCHAR(30) NOT NULL,
                message TEXT NOT NULL,
                created_at {timestamp} NOT NULL,
                updated_at {timestamp} NOT NULL
            )
        """)
        connection.exec_driver_sql("CREATE INDEX ix_jobs_status ON jobs (status)")
