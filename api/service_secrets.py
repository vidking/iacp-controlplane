"""Sobres cifrados; la clave maestra se monta fuera de PostgreSQL."""

import hashlib
import hmac
import json
import os
import secrets
from functools import lru_cache
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


@lru_cache(maxsize=4)
def _cipher_for(path: str, mtime_ns: int, size: int) -> Fernet:
    return Fernet(Path(path).read_bytes().strip())


def cipher() -> Fernet:
    path = os.getenv("IACP_SERVICE_KEY_FILE", "")
    if not path:
        raise RuntimeError("SERVICE_KEY_NOT_CONFIGURED")
    # La caché se invalida si la clave se rota (cambia mtime o tamaño).
    stat = os.stat(path)
    return _cipher_for(path, stat.st_mtime_ns, stat.st_size)


def signer_token(path: str | None = None) -> str:
    """Credencial derivada para la firma de certificados TLS.

    Los workers la presentan como bearer en lugar de la clave maestra: si el
    token se filtra (logs, proxy, captura de red), no permite descifrar los
    secretos de servicios. Es determinista, así que API y workers coinciden
    sin configuración adicional.
    """
    key_path = path or os.getenv("IACP_SERVICE_KEY_FILE", "")
    if not key_path:
        raise RuntimeError("SERVICE_KEY_NOT_CONFIGURED")
    key = Path(key_path).read_bytes().strip()
    if not key:
        raise RuntimeError("SERVICE_KEY_NOT_CONFIGURED")
    return hmac.new(key, b"iacp-service-tls-signer-v1", hashlib.sha256).hexdigest()


def encrypt(value: dict) -> str:
    return cipher().encrypt(json.dumps(value).encode()).decode()


def decrypt(value: str) -> dict:
    return json.loads(cipher().decrypt(value.encode()))


def installation_credentials() -> dict:
    def keypair(comment: str) -> tuple[str, str]:
        key = ed25519.Ed25519PrivateKey.generate()
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
        public = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
            )
            .decode()
            + f" {comment}"
        )
        return private, public

    installer_private, installer_public = keypair("iacp-installer")
    maintenance_private, maintenance_public = keypair("iacp-maintenance")
    return {
        "ssh_private_key": installer_private,
        "ssh_public_key": installer_public,
        "management_ssh_private_key": maintenance_private,
        "management_ssh_public_key": maintenance_public,
        "admin_password": secrets.token_hex(32),
        "database_password": secrets.token_hex(32),
        "encryption_key": secrets.token_hex(32),
        "gateway_token": secrets.token_hex(32),
    }
