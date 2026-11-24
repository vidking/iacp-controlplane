"""Emisión de certificados IP; la clave privada de la CA solo vive en la API."""

from datetime import datetime, timedelta, timezone
import os
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def issue(address: str, *, allow_non_global: bool = False) -> dict[str, str]:
    if os.getenv("IACP_SERVICE_IP_TLS_ENABLED", "0") != "1":
        raise ValueError("SERVICE_IP_TLS_DISABLED")
    ip = ip_address(address)
    if not ip.is_global and not allow_non_global:
        raise ValueError("SERVICE_IP_TLS_ADDRESS_INVALID")
    certificate_path = Path(os.environ["IACP_SERVICE_CA_CERT_FILE"])
    key_path = Path(os.environ["IACP_SERVICE_CA_KEY_FILE"])
    ca_certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    ca_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    now = datetime.now(timezone.utc)
    constraints = ca_certificate.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value
    if (
        not constraints.ca
        or ca_certificate.not_valid_after_utc <= now + timedelta(days=7)
        or ca_certificate.public_key().public_numbers()
        != ca_key.public_key().public_numbers()
    ):
        raise ValueError("SERVICE_CA_INVALID")
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, str(ip))])
        )
        .issuer_name(ca_certificate.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return {
        "pilot.crt": leaf.public_bytes(serialization.Encoding.PEM).decode(),
        "pilot.key": leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "pilot-ca.crt": ca_certificate.public_bytes(serialization.Encoding.PEM).decode(),
    }
