import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from iacp.api.dependencies import get_smoke_publisher
from iacp.db import get_session
from iacp.main import create_app
from tests.helpers import upgrade


@pytest.fixture
def empty_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    yield engine
    engine.dispose()


@pytest.fixture
def engine(empty_engine):
    upgrade(empty_engine)
    return empty_engine


@pytest.fixture
def sessions(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def auth(tmp_path, monkeypatch):
    token = secrets.token_hex(32)
    path = tmp_path / "api_token"
    path.write_text(token)
    monkeypatch.setenv("IACP_API_TOKEN_FILE", str(path))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def application(sessions):
    application = create_app()

    def session_override():
        with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = session_override
    application.dependency_overrides[get_smoke_publisher] = lambda: lambda job_id: None
    return application


@pytest.fixture
def client(application):
    with TestClient(application) as client:
        yield client
