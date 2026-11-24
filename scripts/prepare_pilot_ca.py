#!/usr/bin/env python3
"""Crear una CA privada de pruebas sin sobrescribir certificados existentes."""

import argparse
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def prepare(directory: Path, uid: int = 999, gid: int = 999, validity_days: int = 3650):
    if type(validity_days) is not int or not 365 <= validity_days <= 3650:
        raise ValueError("La vigencia de CA debe estar entre 365 y 3650 días")
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name(
        [x509.NameAttribute(x509.NameOID.COMMON_NAME, "IACP CA exclusiva del piloto")]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    files = [
        (
            "pilot-ca.key",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        ),
        ("pilot-ca.crt", cert.public_bytes(serialization.Encoding.PEM)),
    ]
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, directory_flags)
    except OSError as error:
        raise ValueError("El directorio de la CA debe existir y no ser un enlace") from error
    created = []
    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_mode & 0o077:
            raise ValueError("El directorio de la CA debe tener permisos privados")
        os.fchown(directory_fd, uid, gid)
        for name, _data in files:
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("CA existente; no sobrescribir")
        for name, data in files:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
            created.append(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fchown(handle.fileno(), uid, gid)
    except Exception:
        for name in created:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(directory_fd)
    print("CA del piloto creada; clave privada conservada en el directorio protegido")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--validity-days", type=int, default=3650)
    args = parser.parse_args()
    prepare(args.directory, validity_days=args.validity_days)
