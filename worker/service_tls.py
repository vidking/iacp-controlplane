"""Certificados IP del piloto; la CA privada permanece en el Control Plane."""

import os
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def issue(address: str) -> dict[str, str]:
    if os.getenv("IACP_SERVICE_IP_TLS_ENABLED", "0") != "1":
        raise ValueError("SERVICE_IP_TLS_DISABLED")
    root = Path(os.getenv("IACP_SERVICE_CA_DIR", "/run/iacp-services"))
    cert = x509.load_pem_x509_certificate((root / "pilot-ca.crt").read_bytes())
    ca_key = serialization.load_pem_private_key(
        (root / "pilot-ca.key").read_bytes(), password=None
    )
    now = datetime.now(timezone.utc)
    if not cert.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value.ca or cert.not_valid_after_utc <= now + timedelta(days=7):
        raise ValueError("SERVICE_CA_INVALID")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, address)])
        )
        .issuer_name(cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address(address))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return {
        "pilot.crt": leaf.public_bytes(serialization.Encoding.PEM).decode(),
        "pilot.key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "pilot-ca.crt": cert.public_bytes(serialization.Encoding.PEM).decode(),
    }
