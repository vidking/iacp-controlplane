import os
from pathlib import Path
from urllib.parse import quote_plus
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


def secret(name: str) -> str:
    p = os.environ[name]
    return Path(p).read_text(encoding="utf8").strip()


def database_url() -> str:
    u = quote_plus(os.environ.get("POSTGRES_USER", "iacp"))
    pw = quote_plus(secret("DATABASE_PASSWORD_FILE"))
    db = quote_plus(os.environ.get("POSTGRES_DB", "iacp"))
    host = os.environ.get("DATABASE_HOST", "db")
    return f"postgresql+psycopg://{u}:{pw}@{host}:5432/{db}"


engine = create_engine(database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()
