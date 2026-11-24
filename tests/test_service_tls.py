from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from api.service_tls import issue


@pytest.mark.no_database
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
    monkeypatch.setenv("IACP_SERVICE_CA_CERT_FILE", str(tmp_path / "pilot-ca.crt"))
    monkeypatch.setenv("IACP_SERVICE_CA_KEY_FILE", str(tmp_path / "pilot-ca.key"))
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


@pytest.mark.no_database
def test_api_signer_uses_only_active_service_job_binding(tmp_path, monkeypatch):
    from api import main

    key_path = tmp_path / "service-key"
    key_path.write_text("stack-service-auth-key")
    monkeypatch.setenv("IACP_SERVICE_KEY_FILE", str(key_path))
    monkeypatch.setenv("IACP_SERVICES_ENABLED", "1")
    monkeypatch.setenv("IACP_SERVICE_IP_TLS_ENABLED", "1")
    row = {
        "job_id": "job-active",
        "action": "install",
        "job_status": "running",
        "worker_id": "service-worker-1",
        "service_id": "service-1",
        "application_id": "nextcloud",
        "service_status": "installing",
        "configuration_json": json.dumps({"access_mode": "ip"}),
        "vmid": 40001,
        "infrastructure_status": "ready",
        "binding_json": json.dumps({
            "address": "190.0.231.2",
            "source": "qemu-guest-agent",
            "vmid": 40001,
        }),
    }

    class FakeDatabase:
        def execute(self, *_args):
            return SimpleNamespace(fetchone=lambda: row)

    @contextmanager
    def database():
        yield FakeDatabase()

    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(
        main,
        "issue_service_tls_certificate",
        lambda address: {"issued_for": address},
    )
    request = Request({
        "type": "http",
        "headers": [(b"authorization", b"Bearer stack-service-auth-key")],
    })
    assert main.issue_service_job_tls_certificate("job-active", request) == {
        "issued_for": "190.0.231.2"
    }

    row["job_status"] = "uncertain"
    with pytest.raises(HTTPException, match="SERVICE_SIGNER_JOB_NOT_ACTIVE"):
        main.issue_service_job_tls_certificate("job-active", request)
