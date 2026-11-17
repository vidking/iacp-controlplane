"""Sobres cifrados; la clave maestra se monta fuera de PostgreSQL."""

import json
import os
import uuid
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def cipher() -> Fernet:
    path = os.getenv("IACP_SERVICE_KEY_FILE", "")
    if not path:
        raise RuntimeError("SERVICE_KEY_NOT_CONFIGURED")
    return Fernet(Path(path).read_bytes().strip())


def encrypt(value: dict) -> str:
    return cipher().encrypt(json.dumps(value).encode()).decode()


def decrypt(value: str) -> dict:
    return json.loads(cipher().decrypt(value.encode()))


def installation_credentials() -> dict:
    key = ed25519.Ed25519PrivateKey.generate()
    return {
        "ssh_private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode(),
        "ssh_public_key": key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        .decode()
        + " iacp-installer",
        "admin_password": uuid.uuid4().hex + uuid.uuid4().hex,
        "database_password": uuid.uuid4().hex + uuid.uuid4().hex,
        "encryption_key": uuid.uuid4().hex + uuid.uuid4().hex,
        "gateway_token": uuid.uuid4().hex + uuid.uuid4().hex,
    }
