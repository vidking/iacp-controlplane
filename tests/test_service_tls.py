from datetime import datetime, timedelta, timezone
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from worker.service_tls import issue


def test_pilot_ca_signs_exact_ip_and_never_returns_ca_key(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "CA piloto")])
    now = datetime.now(timezone.utc)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (tmp_path / "pilot-ca.key").write_bytes(private)
    (tmp_path / "pilot-ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setenv("IACP_SERVICE_CA_DIR", str(tmp_path))
    monkeypatch.setenv("IACP_SERVICE_IP_TLS_ENABLED", "1")
    files = issue("190.0.231.2")
    leaf = x509.load_pem_x509_certificate(files["pilot.crt"].encode())
    leaf.verify_directly_issued_by(ca)
    assert (
        str(
            leaf.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.IPAddress)[0]
        )
        == "190.0.231.2"
    )
    assert all(private.decode() != value for value in files.values())
    monkeypatch.setenv("IACP_SERVICE_IP_TLS_ENABLED", "0")
    with pytest.raises(ValueError, match="DISABLED"):
        issue("190.0.231.2")
