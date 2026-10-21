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
