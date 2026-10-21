import importlib.util
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_tls", ROOT / "scripts/render-tls.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def policy():
    return {"lan_cidrs": ["192.0.2.0/24"], "crm_source_cidrs": ["198.51.100.10/32"],
            "crm_origins": ["https://crm.fixture.invalid"]}


def test_tls_is_opt_in_and_preserves_private_services():
    base = yaml.safe_load((ROOT / "compose.yaml").read_text())
    override = yaml.safe_load((ROOT / "compose.tls.yaml").read_text())
    assert set(override["services"]) == {"traefik"}
    assert not override.get("volumes")
    assert override["networks"]["proxy_network"]["external"] is True
    assert ":?" in override["networks"]["proxy_network"]["name"]
    assert base["services"]["traefik"]["ports"] == ["127.0.0.1:8080:8080"]
    assert ":?" in override["services"]["traefik"]["ports"][0]
    for role in ("db", "redis", "worker", "api"):
        assert not base["services"][role].get("ports")
    assert all(mount.get("read_only") for mount in override["services"]["traefik"]["volumes"] if isinstance(mount, dict))
    static = yaml.safe_load((ROOT / "traefik/tls.yml").read_text())
    assert static["entryPoints"]["web"]["address"] == ":8080"
    assert static["entryPoints"]["websecure"]["forwardedHeaders"]["insecure"] is False
    assert static["accessLog"]["fields"]["headers"]["defaultMode"] == "drop"


@pytest.mark.parametrize("field,value", [("lan_cidrs", []), ("crm_source_cidrs", ["0.0.0.0/0"]),
    ("lan_cidrs", ["::/0"]), ("crm_origins", ["*"]), ("crm_origins", ["http://crm.invalid"]),
    ("crm_origins", ["https://crm.invalid/path"]), ("crm_origins", ["https://user:secret@crm.invalid"]),
    ("crm_origins", ["https://crm.invalid`) || PathPrefix(`/"]), ("crm_source_cidrs", ["not-authorized"])])
def test_tls_policy_rejects_missing_or_unsafe_values(field, value):
    with pytest.raises(ValueError):
        renderer.build({**policy(), field: value})


def test_tls_uses_official_host_external_certificates_and_transport_ip():
    configuration = renderer.build(policy())
    router = configuration["http"]["routers"]["iacp-https"]
    assert 'Host(`iac.cyberfuel.com`)' in router["rule"]
    assert router["tls"]["options"] == "corporate"
    allow = configuration["http"]["middlewares"]["authorized-sources"]["ipAllowList"]
    assert "ipStrategy" not in allow
    assert configuration["tls"]["certificates"] == [{"certFile": "/run/tls/fullchain.pem", "keyFile": "/run/tls/privkey.pem"}]


def test_generator_validates_certificate_pair_and_writes_no_private_material(tmp_path, monkeypatch):
    import json
    import ssl
    certificates = tmp_path / "certificates"
    certificates.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=iac.cyberfuel.com", "-addext", "subjectAltName=DNS:iac.cyberfuel.com",
                    "-keyout", str(certificates / "privkey.pem"), "-out", str(certificates / "fullchain.pem")],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy()))
    destination = tmp_path / "dynamic"
    argv = ["render-tls", "--policy", str(policy_file), "--output-dir", str(destination),
            "--certificates", str(certificates), "--bind-ip", "10.23.45.67"]
    monkeypatch.setattr("sys.argv", argv)
    renderer.main()
    output = destination / "iacp.yml"
    assert output.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE KEY" not in output.read_text()
    assert yaml.safe_load(output.read_text())["tls"]["certificates"][0]["keyFile"] == "/run/tls/privkey.pem"
    monkeypatch.setattr("sys.argv", argv[:-1] + ["0.0.0.0"])
    with pytest.raises(ValueError): renderer.main()
    (certificates / "privkey.pem").write_text("invalid key")
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(ssl.SSLError): renderer.main()
