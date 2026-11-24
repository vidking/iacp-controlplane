#!/usr/bin/env python3
"""Crear una CA privada de pruebas sin sobrescribir certificados existentes."""

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def prepare(directory: Path, uid: int = 999, gid: int = 999, validity_days: int = 3650):
    if not directory.is_dir() or any(
        (directory / name).exists() for name in ["pilot-ca.key", "pilot-ca.crt"]
    ):
        raise ValueError("Directorio ausente o CA existente; no sobrescribir")
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
    for name, data in [
        (
            "pilot-ca.key",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        ),
        ("pilot-ca.crt", cert.public_bytes(serialization.Encoding.PEM)),
    ]:
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chown(path, uid, gid)
    print("CA del piloto creada; clave privada conservada en el directorio protegido")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--validity-days", type=int, default=3650)
    args = parser.parse_args()
    prepare(args.directory, validity_days=args.validity_days)
