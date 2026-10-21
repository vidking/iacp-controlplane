from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_preserves_storage_and_waits_for_migrations():
    config = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(config["volumes"]) == {"postgres_data", "redis_data"}
    services = config["services"]
    assert services["db"]["volumes"] == ["postgres_data:/var/lib/postgresql/data"]
    assert services["redis"]["volumes"] == ["redis_data:/data"]
    for role in ("api", "worker"):
        assert services[role]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["traefik"]["ports"] == ["127.0.0.1:8080:8080"]


def test_test_stack_cannot_mount_runtime_storage_or_publish_ports():
    config = yaml.safe_load((ROOT / "compose.test.yaml").read_text())
    assert not config.get("volumes")
    assert config["networks"]["test"]["internal"] is True
    for service in config["services"].values():
        assert not service.get("volumes")
        assert not service.get("ports")


def test_development_is_separate_and_uses_its_own_secret_volume():
    runtime = yaml.safe_load((ROOT / "compose.yaml").read_text())
    dev = yaml.safe_load((ROOT / "compose.dev.yaml").read_text())
    assert dev["name"] != runtime["name"]
    assert set(dev["services"]).isdisjoint(runtime["services"])
    assert set(dev["volumes"]).isdisjoint(runtime["volumes"])
    assert not dev.get("secrets")
    assert dev["services"]["dev-init-secrets"]["network_mode"] == "none"
    for role in ("dev-api", "dev-worker", "dev-migrate"):
        config = dev["services"][role]
        assert "bootstrap" not in config["image"]
        assert config["environment"]["DATABASE_HOST"] == "dev-db"
        assert config["volumes"] == ["dev_secrets:/run/dev-secrets:ro"]
    for service in dev["services"].values():
        assert not service.get("privileged")
        for mount in service.get("volumes", []):
            assert "docker.sock" not in mount
            assert not mount.startswith("/")


def test_server_transition_requires_explicit_existing_resources():
    server = yaml.safe_load((ROOT / "compose.server.yaml").read_text())
    for volume in ("postgres_data", "redis_data"):
        assert server["volumes"][volume]["external"] is True
        assert ":?" in server["volumes"][volume]["name"]
    for secret in server["secrets"].values():
        assert ":?" in secret["file"]
    assert set(server["services"]) == {"api", "worker", "migrate"}
    for service in server["services"].values():
        assert "${IACP_RELEASE:?" in service["image"]
