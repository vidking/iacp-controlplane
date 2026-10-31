from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from api.db import connect, initialize


@pytest.fixture(scope="session", autouse=True)
def technical_database():
    try:
        with connect():
            pass
    except Exception as error:
        pytest.skip(f"PostgreSQL técnico no disponible: {type(error).__name__}")
    initialize()


@pytest.fixture(autouse=True)
def technical_secrets(tmp_path, monkeypatch):
    key_path = tmp_path / "access.key"
    key_path.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("IACP_ACCESS_ENCRYPTION_KEY_FILE", str(key_path))
    monkeypatch.setenv("IACP_API_TOKEN", "technical-test-token")
