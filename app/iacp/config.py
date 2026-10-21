"""Runtime configuration. Secrets stay in files and are never read at import."""

import os
from pathlib import Path

from sqlalchemy import URL


def secret(name: str) -> str:
    return Path(os.environ[name]).read_text(encoding="utf8").strip()


def database_url() -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=os.environ.get("POSTGRES_USER", "iacp"),
        password=secret("DATABASE_PASSWORD_FILE"),
        host=os.environ.get("DATABASE_HOST", "db"),
        port=int(os.environ.get("DATABASE_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "iacp"),
    )


def broker_url() -> str:
    return os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
