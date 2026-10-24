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


def test_optional_tenant_credentials_are_external_and_only_mounted_in_api():
    config = yaml.safe_load((ROOT / "compose.tenants.yaml").read_text())
    assert set(config["services"]) == {"api"}
    assert config["services"]["api"]["environment"] == {"IACP_TENANT_CREDENTIALS_FILE": "/run/secrets/tenant_credentials"}
    assert ":?" in config["secrets"]["tenant_credentials"]["file"]
    assert not config.get("volumes")


def test_terraform_worker_is_opt_in_nonprivileged_and_real_plan_is_disabled():
    baseline = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert "terraform-worker" not in baseline["services"]
    override = yaml.safe_load((ROOT / "compose.terraform.yaml").read_text())
    worker = override["services"]["terraform-worker"]
    assert worker["build"]["target"] == "terraform-worker"
    assert worker["environment"]["IACP_TERRAFORM_PLAN_ENABLED"] == "${IACP_TERRAFORM_PLAN_ENABLED:-0}"
    assert worker["read_only"] is True
    assert worker["cap_drop"] == ["ALL"]
    assert all(mount.get("read_only") for mount in worker["volumes"] if isinstance(mount, dict))
    assert "api_token" not in worker["secrets"]
    assert override["volumes"]["terraform_plans"]["external"] is True


def test_canonical_guest_identity_worker_has_shared_gate_without_expanding_lifecycle_secrets():
    services=yaml.safe_load((ROOT/'deploy/mng/compose.yaml').read_text())['services']
    for role in ['api','terraform-worker','lifecycle-worker']:
        assert '${IACP_KVM_LIFECYCLE_ENABLED:?' in services[role]['environment']['IACP_KVM_LIFECYCLE_ENABLED']
    lifecycle=services['lifecycle-worker']
    assert 'IACP_TERRAFORM_PROFILES_FILE' not in lifecycle['environment']
    assert {v['source'] for v in lifecycle['secrets']}=={'db_password','kvm_credentials'}
