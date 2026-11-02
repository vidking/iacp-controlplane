import json
from pathlib import Path
import stat

import pytest

from scripts import go_live


IMAGE_TAG = "a" * 40
IMAGE_REPOSITORY = "scm.cyberfuel.com/cyberfuel/iacp-controlplane"


def _configuration(*, stage="production", bucket="cyberfuel-iacp-tfstate-lab", prefix="iacp/prod-runtime-v1", include_aws=True):
    backend = "s3" if stage == "production" else "local"
    database_name = "iacp_controlplane" if stage == "production" else "iacp_controlplane_dev"
    database_password = "postgres-password-with-entropy"
    api_environment = {
        "IACP_RUNTIME_STAGE": "production" if stage == "production" else "develop",
        "IACP_DEFAULT_ENVIRONMENT": "lab",
        "IACP_ENABLED_ENVIRONMENTS": "lab",
        "IACP_PRX_ENABLED": "0",
        "IACP_MAX_CONCURRENT": "1",
        "IACP_REQUIRE_API_TOKEN": "1",
        "PGHOST": "postgres",
        "PGPORT": "5432",
        "PGDATABASE": database_name,
        "PGUSER": "iacp_test",
        "PGPASSWORD": database_password,
        "IACP_API_TOKEN": "api-token-with-sufficient-entropy",
        "IACP_PROXMOX_MNG_API_TOKEN": "proxmox-token-with-entropy",
        "IACP_TERRAFORM_BACKEND": backend,
        "IACP_TFSTATE_BUCKET": bucket if stage == "production" else "",
        "IACP_TFSTATE_PREFIX": prefix if stage == "production" else "",
        "IACP_PROXY_NETWORK": "proxy_network",
        "IACP_BUILD_REVISION": IMAGE_TAG,
    }
    worker_environment = {
        "PGHOST": "postgres",
        "PGPORT": "5432",
        "PGDATABASE": database_name,
        "PGUSER": "iacp_test",
        "PGPASSWORD": database_password,
    }
    if include_aws:
        worker_environment.update({"AWS_ACCESS_KEY_ID": "access-key-value", "AWS_SECRET_ACCESS_KEY": "secret-key-value"})
    services = {
        name: {"image": f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"}
        for name in ("api", "worker-1", "worker-2")
    }
    services["api"]["environment"] = api_environment
    services["postgres"] = {
        "environment": {
            "POSTGRES_DB": database_name,
            "POSTGRES_USER": "iacp_test",
            "POSTGRES_PASSWORD": database_password,
        }
    }
    alias = "iacp-controlplane-api-prod" if stage == "production" else "iacp-controlplane-api-dev"
    services["api"]["networks"] = {"proxy": {"aliases": [alias]}}
    services["api"]["extra_hosts"] = ["mng01.cyberfuel.com=192.0.2.10"]
    services["worker-1"]["environment"] = worker_environment
    services["worker-2"]["environment"] = worker_environment
    services["worker-1"]["extra_hosts"] = ["mng01.cyberfuel.com=192.0.2.10"]
    services["worker-2"]["extra_hosts"] = ["mng01.cyberfuel.com=192.0.2.10"]
    volume_suffix = "prod" if stage == "production" else "dev"
    return {
        "services": services,
        "networks": {"proxy": {"name": "proxy_network"}},
        "volumes": {
            "postgres_data": {"name": f"iacp_controlplane_{volume_suffix}_postgres"},
            "workdata": {"name": f"iacp_controlplane_{volume_suffix}_workdata"},
        },
    }


def _manifest(stage="production", **overrides):
    result = {
        "stage": stage,
        "project": "iacp-controlplane-prod-v1" if stage == "production" else "iacp-controlplane-dev-v1",
        "domain": "iac.cyberfuel.com" if stage == "production" else "dev.iac.cyberfuel.com",
        "service_alias": "iacp-controlplane-api-prod" if stage == "production" else "iacp-controlplane-api-dev",
        "proxy_network": "proxy_network",
        "backend": "s3" if stage == "production" else "local",
        "state_bucket": "cyberfuel-iacp-tfstate-lab" if stage == "production" else "",
        "state_prefix": "iacp/prod-runtime-v1" if stage == "production" else "",
        "max_concurrent": 1,
        "image_repository": IMAGE_REPOSITORY,
        "image_tag": IMAGE_TAG,
    }
    result.update(overrides)
    return result


def _private_env(tmp_path):
    path = tmp_path / "runtime.env"
    path.write_text("PRIVATE_TEST_VALUE=not-a-secret\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_production_configuration_passes_policy(tmp_path):
    summary = go_live._check_policy(_manifest(), _configuration(), _private_env(tmp_path))
    assert summary["backend"] == "s3"
    assert summary["state_prefix"] == "iacp/prod-runtime-v1"
    assert summary["enabled_environments"] == ["lab"]
    assert summary["image_tag"] == IMAGE_TAG


def test_development_rejects_wasabi_credentials(tmp_path):
    with pytest.raises(go_live.GateError, match="no debe recibir credenciales Wasabi"):
        go_live._check_policy(
            _manifest("develop"),
            _configuration(stage="develop", include_aws=True),
            _private_env(tmp_path),
        )


def test_development_configuration_passes_without_wasabi(tmp_path):
    summary = go_live._check_policy(
        _manifest("develop"),
        _configuration(stage="develop", include_aws=False),
        _private_env(tmp_path),
    )
    assert summary["backend"] == "local"
    assert summary["state_prefix"] == ""
    assert summary["prx_enabled"] is False


def test_production_rejects_unexpected_state_prefix(tmp_path):
    manifest = _manifest(state_prefix="iac/old")
    with pytest.raises(go_live.GateError, match="prefijo de state"):
        go_live._check_policy(manifest, _configuration(), _private_env(tmp_path))


def test_env_file_must_be_mode_600(tmp_path):
    path = _private_env(tmp_path)
    path.chmod(0o640)
    with pytest.raises(go_live.GateError, match="permisos 600"):
        go_live._check_policy(_manifest(), _configuration(), path)


def test_evidence_directory_is_private_and_outside_checkout(tmp_path):
    path = go_live._new_evidence_dir(tmp_path / "evidence")
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    with pytest.raises(go_live.GateError, match="fuera del checkout"):
        go_live._new_evidence_dir(go_live.ROOT / "tests" / "evidence-forbidden")


def test_external_health_requires_the_candidate_revision(monkeypatch):
    class Response:
        status = 200
        headers = {"X-IACP-Build-Revision": IMAGE_TAG}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"status": "ready"}).encode()

    monkeypatch.setattr(go_live, "urlopen", lambda *args, **kwargs: Response())
    assert go_live._check_external("iac.cyberfuel.com", IMAGE_TAG)["image_tag"] == IMAGE_TAG
    with pytest.raises(go_live.GateError, match="revisión candidata"):
        go_live._check_external("iac.cyberfuel.com", "b" * 40, retries=0)


def test_external_health_retries_until_traefik_serves_the_candidate(monkeypatch):
    revisions = iter(("legacy", IMAGE_TAG))

    class Response:
        status = 200

        def __init__(self, revision):
            self.headers = {"X-IACP-Build-Revision": revision}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"status": "ready"}).encode()

    monkeypatch.setattr(go_live, "urlopen", lambda *args, **kwargs: Response(next(revisions)))
    monkeypatch.setattr(go_live.time, "sleep", lambda *_: None)
    result = go_live._check_external("iac.cyberfuel.com", IMAGE_TAG)
    assert result["image_tag"] == IMAGE_TAG


def test_verify_checks_runtime_contract_and_served_revision(monkeypatch):
    services = [
        {"service": name, "state": "running", "health": "healthy" if name in {"postgres", "api"} else "", "image": f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"}
        for name in ("postgres", "api", "worker-1", "worker-2")
    ]
    monkeypatch.setattr(go_live, "_inventory", lambda *args: {"containers": services})
    monkeypatch.setattr(
        go_live,
        "_run",
        lambda *args, **kwargs: json.dumps({
            "health": "ready",
            "api_version": "1.0.0",
            "provision_path": True,
            "build_revision": IMAGE_TAG,
        }),
    )
    result = go_live._verify({"project": "iacp-controlplane-prod-v1", "image_tag": IMAGE_TAG}, Path("compose.yml"), Path("runtime.env"))
    assert result["api"]["build_revision"] == IMAGE_TAG


def test_upstream_swap_is_atomic_and_preserves_the_previous_route(tmp_path):
    route = tmp_path / "iacp.yml"
    evidence = go_live._new_evidence_dir(tmp_path / "evidence")
    route.write_text(
        "http:\n  services:\n    iacp-api:\n      loadBalancer:\n        servers:\n          - url: http://iacp-controlplane-api-prod:8080\n",
        encoding="utf-8",
    )
    route.chmod(0o640)
    result = go_live._swap_upstream(route, evidence, "http://iacp-controlplane-api-prod:8080", "http://api:8000")
    assert result["changed"] is True
    assert "url: http://api:8000" in route.read_text(encoding="utf-8")
    assert "url: http://iacp-controlplane-api-prod:8080" in (evidence / "traefik-before.yml").read_text(encoding="utf-8")
    assert stat.S_IMODE(route.stat().st_mode) == 0o640
    assert stat.S_IMODE((evidence / "traefik-before.yml").stat().st_mode) == 0o600


def test_upstream_swap_rejects_unexpected_route_without_changes(tmp_path):
    route = tmp_path / "iacp.yml"
    evidence = go_live._new_evidence_dir(tmp_path / "evidence")
    original = "http:\n  services:\n    iacp-api:\n      loadBalancer:\n        servers:\n          - url: http://operator-changed-upstream:8000\n"
    route.write_text(original, encoding="utf-8")
    with pytest.raises(go_live.GateError, match="upstream activo cambió"):
        go_live._swap_upstream(route, evidence, "http://iacp-controlplane-api-prod:8080", "http://api:8000")
    assert route.read_text(encoding="utf-8") == original
    assert not (evidence / "traefik-before.yml").exists()


def test_legacy_restore_gate_reads_docker_health_status(monkeypatch):
    monkeypatch.setattr(
        go_live,
        "_run",
        lambda *args, **kwargs: json.dumps({"State": "running", "HealthStatus": "healthy"}),
    )
    assert go_live._require_legacy_api_healthy() is None


def test_legacy_restore_gate_stops_when_api_is_unhealthy(monkeypatch):
    monkeypatch.setattr(
        go_live,
        "_run",
        lambda *args, **kwargs: json.dumps({"State": "running", "HealthStatus": "starting"}),
    )
    with pytest.raises(go_live.GateError, match="API legacy no está única"):
        go_live._require_legacy_api_healthy()
