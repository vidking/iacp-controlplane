"""Cifrado efímero de credenciales técnicas de Guest Initialization."""

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _key() -> bytes:
    path = os.getenv("IACP_ACCESS_ENCRYPTION_KEY_FILE", "/run/secrets/access_encryption_key")
    raw = Path(path).read_bytes().strip()
    if not raw:
        raise RuntimeError("ACCESS_ENCRYPTION_KEY_MISSING")
    return raw


def encrypt_secret(value: str) -> str:
    return Fernet(_key()).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return Fernet(_key()).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as error:
        raise RuntimeError("ACCESS_SECRET_INVALID") from error
