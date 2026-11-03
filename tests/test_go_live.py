import json
from pathlib import Path
import re
import stat
from urllib.error import HTTPError

import pytest

from scripts import go_live


IMAGE_TAG = "a" * 40
IMAGE_REPOSITORY = "scm.cyberfuel.com/cyberfuel/iacp-controlplane"


def test_local_compose_declares_six_workers_with_shared_capacity():
    compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text(encoding="utf-8")
    workers = re.findall(r"(?m)^  (worker-\d+):$", compose)
    assert workers == [f"worker-{index}" for index in range(1, 7)]
    assert "IACP_MAX_CONCURRENT: ${IACP_MAX_CONCURRENT:-6}" in compose
    for worker in workers:
        block = re.search(
            rf"(?ms)^  {worker}:\n(.*?)(?=^  [a-zA-Z][\w-]*:\n|^volumes:|\Z)",
            compose,
        )
        assert block is not None
        assert f'"--worker-id", "{worker}"' in block.group(1)
        assert "IACP_WORKDATA_DIR: /var/lib/iacp-controlplane" in block.group(1)


def _configuration(*, stage="production", bucket="iacp-cyberfuel", prefix="iacp/prod-runtime-v1", include_aws=True):
    wasabi = stage == "production"
    database_name = "iacp_controlplane" if stage == "production" else "iacp_controlplane_dev"
    database_password = "postgres-password-with-entropy"
    api_environment = {
        "IACP_RUNTIME_STAGE": "production" if stage == "production" else "develop",
        "IACP_DEFAULT_ENVIRONMENT": "lab",
        "IACP_ENABLED_ENVIRONMENTS": "lab",
        "IACP_PRX_ENABLED": "0",
        "IACP_MAX_CONCURRENT": "6",
        "IACP_VMID_MIN": "30000" if stage == "production" else "40000",
        "IACP_VMID_MAX": "39999" if stage == "production" else "49999",
        "IACP_REQUIRE_API_TOKEN": "1",
        "PGHOST": "postgres",
        "PGPORT": "5432",
        "PGDATABASE": database_name,
        "PGUSER": "iacp_test",
        "PGPASSWORD": database_password,
        "IACP_API_TOKEN": "api-token-with-sufficient-entropy",
        "IACP_PROXMOX_MNG_API_TOKEN": "proxmox-token-with-entropy",
        "IACP_PROXMOX_MNG_ENDPOINT": "https://mng01.cyberfuel.com:8006/",
        "IACP_PROXMOX_API_TOKEN": "proxmox-token-with-entropy",
        "IACP_PROXMOX_ENDPOINT": "https://mng01.cyberfuel.com:8006/",
        "IACP_TERRAFORM_DIR": "environments/mng01",
        "IACP_WASABI_ENABLED": "true" if wasabi else "false",
        "IACP_TFSTATE_BUCKET": bucket if wasabi else "",
        "IACP_TFSTATE_ENDPOINT": "https://s3.us-east-1.wasabisys.com" if wasabi else "",
        "IACP_TFSTATE_REGION": "us-east-1" if wasabi else "",
        "IACP_TFSTATE_PREFIX": prefix if wasabi else "",
        "IACP_PROXY_NETWORK": "proxy_network",
        "IACP_BUILD_REVISION": IMAGE_TAG,
    }
    worker_environment = {
        "PGHOST": "postgres",
        "PGPORT": "5432",
        "PGDATABASE": database_name,
        "PGUSER": "iacp_test",
        "PGPASSWORD": database_password,
        "IACP_MAX_CONCURRENT": "6",
        "IACP_PROXMOX_MNG_API_TOKEN": "proxmox-token-with-entropy",
        "IACP_PROXMOX_MNG_ENDPOINT": "https://mng01.cyberfuel.com:8006/",
        "IACP_PROXMOX_API_TOKEN": "proxmox-token-with-entropy",
        "IACP_PROXMOX_ENDPOINT": "https://mng01.cyberfuel.com:8006/",
        "IACP_TERRAFORM_DIR": "environments/mng01",
        "IACP_VMID_MIN": "30000" if stage == "production" else "40000",
        "IACP_VMID_MAX": "39999" if stage == "production" else "49999",
    }
    worker_environment["IACP_WASABI_ENABLED"] = "true" if wasabi else "false"
    if include_aws and wasabi:
        worker_environment.update({"IACP_WASABI_ACCESS_KEY_ID": "access-key-value", "IACP_WASABI_SECRET_ACCESS_KEY": "secret-key-value"})
    worker_names = tuple(f"worker-{number}" for number in range(1, 7))
    services = {
        name: {"image": f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"}
        for name in ("api", *worker_names)
    }
    services["api"]["environment"] = api_environment
    services["postgres"] = {
        "environment": {
            "POSTGRES_DB": database_name,
            "POSTGRES_USER": "iacp_test",
            "POSTGRES_PASSWORD": database_password,
        },
        "networks": {"internal": {}},
    }
    alias = "iacp-controlplane-api-prod" if stage == "production" else "iacp-controlplane-api-dev"
    services["api"]["networks"] = {"internal": {}, "proxy": {"aliases": [alias]}}
    services["api"]["extra_hosts"] = ["mng01.cyberfuel.com=192.0.2.10"]
    for name in worker_names:
        services[name]["environment"] = worker_environment
        services[name]["extra_hosts"] = ["mng01.cyberfuel.com=192.0.2.10"]
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
        "domain": "iac.cyberfuel.com" if stage == "production" else "dev-cyberfuel.com",
        "service_alias": "iacp-controlplane-api-prod" if stage == "production" else "iacp-controlplane-api-dev",
        "proxy_network": "proxy_network",
        "wasabi_enabled": stage == "production",
        "backend": "s3" if stage == "production" else "local",
        "state_bucket": "iacp-cyberfuel" if stage == "production" else "",
        "state_prefix": "iacp/prod-runtime-v1" if stage == "production" else "",
        "max_concurrent": 6,
        "vmid_min": 30000 if stage == "production" else 40000,
        "vmid_max": 39999 if stage == "production" else 49999,
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
    assert summary["vmid_range"] == {"min": 30000, "max": 39999}


def test_development_rejects_wasabi_credentials(tmp_path):
    config = _configuration(stage="develop", include_aws=False)
    for name in (f"worker-{number}" for number in range(1, 7)):
        config["services"][name]["environment"].update({
            "IACP_WASABI_ACCESS_KEY_ID": "unexpected-access",
            "IACP_WASABI_SECRET_ACCESS_KEY": "unexpected-secret",
        })
    with pytest.raises(go_live.GateError, match="no debe recibir credenciales Wasabi"):
        go_live._check_policy(
            _manifest("develop"),
            config,
            _private_env(tmp_path),
        )


def test_stack_rejects_api_and_postgres_on_separate_internal_networks(tmp_path):
    config = _configuration()
    config["services"]["postgres"].pop("networks")

    with pytest.raises(go_live.GateError, match="API y PostgreSQL deben compartir"):
        go_live._check_policy(_manifest(), config, _private_env(tmp_path))


def test_development_configuration_passes_without_wasabi(tmp_path):
    summary = go_live._check_policy(
        _manifest("develop"),
        _configuration(stage="develop", include_aws=False),
        _private_env(tmp_path),
    )
    assert summary["backend"] == "local"
    assert summary["state_prefix"] == ""
    assert summary["prx_enabled"] is False
    assert summary["vmid_range"] == {"min": 40000, "max": 49999}


def test_production_domain_can_run_lab_with_local_state_and_wasabi_disabled(tmp_path, monkeypatch):
    manifest = _manifest(
        wasabi_enabled=False,
        backend="local",
        state_bucket="",
        state_prefix="",
    )
    config = _configuration(stage="production", include_aws=False)
    config["services"]["api"]["environment"].update({
        "IACP_WASABI_ENABLED": "false",
        "IACP_TFSTATE_BUCKET": "",
        "IACP_TFSTATE_ENDPOINT": "",
        "IACP_TFSTATE_REGION": "",
        "IACP_TFSTATE_PREFIX": "",
    })
    for name in (f"worker-{number}" for number in range(1, 7)):
        config["services"][name]["environment"]["IACP_WASABI_ENABLED"] = "false"

    def forbidden_wasabi_request(*args, **kwargs):
        pytest.fail("Wasabi no debe consultarse cuando el interruptor está apagado")

    monkeypatch.setattr(go_live, "urlopen", forbidden_wasabi_request)
    summary = go_live._check_policy(manifest, config, _private_env(tmp_path))

    assert summary["backend"] == "local"
    assert summary["state_prefix"] == ""
    assert go_live._check_runtime_backend(manifest, config) == {
        "backend": "local",
        "remote_state": "deshabilitado",
        "writes_performed": False,
    }


def test_production_rejects_development_vmid_range(tmp_path):
    with pytest.raises(go_live.GateError, match="rango VMID no coincide"):
        go_live._check_policy(
            _manifest(vmid_min=40000, vmid_max=49999),
            _configuration(),
            _private_env(tmp_path),
        )


def test_production_rejects_mismatched_worker_vmid_range(tmp_path):
    config = _configuration()
    config["services"]["worker-2"]["environment"]["IACP_VMID_MIN"] = "40000"
    with pytest.raises(go_live.GateError, match="rango VMID de API y workers"):
        go_live._check_policy(_manifest(), config, _private_env(tmp_path))


def test_production_rejects_unexpected_state_prefix(tmp_path):
    manifest = _manifest(state_prefix="iac/old")
    with pytest.raises(go_live.GateError, match="prefijo de state"):
        go_live._check_policy(manifest, _configuration(), _private_env(tmp_path))


def test_production_rejects_old_wasabi_bucket_name(tmp_path):
    with pytest.raises(go_live.GateError, match="bucket de producción"):
        go_live._check_policy(
            _manifest(state_bucket="cyberfuel-iacp-tfstate-lab"),
            _configuration(bucket="cyberfuel-iacp-tfstate-lab"),
            _private_env(tmp_path),
        )


def test_all_six_workers_share_additional_protected_vmids(tmp_path):
    config = _configuration()
    config["services"]["api"]["environment"]["IACP_ADDITIONAL_PROTECTED_VMIDS"] = "31001,31002"
    config["services"]["worker-4"]["environment"]["IACP_ADDITIONAL_PROTECTED_VMIDS"] = "31001"

    with pytest.raises(go_live.GateError, match="lista de VMID protegidos"):
        go_live._check_policy(_manifest(), config, _private_env(tmp_path))


def test_preflight_rejects_candidate_without_a_dedicated_mng_token(tmp_path):
    config = _configuration()
    config["services"]["api"]["environment"]["IACP_PROXMOX_MNG_API_TOKEN"] = (
        "REEMPLAZAR_CON_TOKEN_MNG_EXCLUSIVO_DEL_CANDIDATO"
    )
    with pytest.raises(go_live.GateError, match="IACP_PROXMOX_MNG_API_TOKEN"):
        go_live._check_policy(_manifest(), config, _private_env(tmp_path))


def test_wasabi_preflight_checks_only_the_approved_prefix(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, *_):
            return b"<ListBucketResult><Name>iacp-cyberfuel</Name><Prefix>iacp/prod-runtime-v1/</Prefix></ListBucketResult>"

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(go_live, "urlopen", fake_urlopen)
    result = go_live._check_wasabi_prefix(_manifest(), _configuration())

    assert result == {
        "bucket": "iacp-cyberfuel",
        "prefix": "iacp/prod-runtime-v1",
        "list_access": "PASS",
        "writes_performed": False,
    }
    assert "prefix=iacp%2Fprod-runtime-v1%2F" in captured["url"]
    assert "Credential=access-key-value/" in captured["authorization"]
    assert "secret-key-value" not in captured["authorization"]
    assert captured["timeout"] == 15


def test_wasabi_preflight_reports_denied_access_without_leaking_credentials(monkeypatch):
    def denied(*args, **kwargs):
        raise HTTPError("https://s3.us-east-1.wasabisys.com/iacp-cyberfuel", 403, "Forbidden", {}, None)

    monkeypatch.setattr(go_live, "urlopen", denied)
    with pytest.raises(go_live.GateError, match="HTTP 403") as error:
        go_live._check_wasabi_prefix(_manifest(), _configuration())
    assert "secret-key-value" not in str(error.value)


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
        for name in ("postgres", "api", *(f"worker-{number}" for number in range(1, 7)))
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
            "schema_version": 2,
            "ip_allocations_table": True,
            "worker_ids": [f"worker-{number}" for number in range(1, 7)],
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


def test_upstream_swap_preserves_json_route_format(tmp_path):
    route = tmp_path / "iacp.yml"
    evidence = go_live._new_evidence_dir(tmp_path / "evidence")
    original = json.dumps(
        {"http": {"services": {"iacp-api": {"loadBalancer": {"servers": [{"url": "http://api:8000"}]}}}}},
        indent=2,
    ) + "\n"
    route.write_text(original, encoding="utf-8")

    result = go_live._swap_upstream(route, evidence, "http://api:8000", "http://iacp-controlplane-api-prod:8080")

    assert result["changed"] is True
    updated = json.loads(route.read_text(encoding="utf-8"))
    assert updated["http"]["services"]["iacp-api"]["loadBalancer"]["servers"][0]["url"] == "http://iacp-controlplane-api-prod:8080"
    assert json.loads((evidence / "traefik-before.yml").read_text(encoding="utf-8")) == json.loads(original)


def test_upstream_swap_rejects_unexpected_route_without_changes(tmp_path):
    route = tmp_path / "iacp.yml"
    evidence = go_live._new_evidence_dir(tmp_path / "evidence")
    original = "http:\n  services:\n    iacp-api:\n      loadBalancer:\n        servers:\n          - url: http://operator-changed-upstream:8000\n"
    route.write_text(original, encoding="utf-8")
    with pytest.raises(go_live.GateError, match="upstream activo cambió"):
        go_live._swap_upstream(route, evidence, "http://iacp-controlplane-api-prod:8080", "http://api:8000")
    assert route.read_text(encoding="utf-8") == original
    assert not (evidence / "traefik-before.yml").exists()


def test_development_route_install_is_safe_and_idempotent(tmp_path):
    dynamic = tmp_path / "dynamic"
    dynamic.mkdir()
    production_route = dynamic / "iacp.yml"
    production_route.write_text(
        "http:\n  services:\n    iacp-api:\n      loadBalancer:\n        servers:\n          - url: http://api:8000\n",
        encoding="utf-8",
    )
    production_route.chmod(0o640)
    route = dynamic / "dev-iac.yml"
    template = go_live.ROOT / "deploy/portainer/traefik-dev.yml"
    evidence = go_live._new_evidence_dir(tmp_path / "install-evidence")

    result = go_live._install_dev_route(route, production_route, template, evidence)

    assert result["changed"] is True
    assert route.read_text(encoding="utf-8") == template.read_text(encoding="utf-8")
    assert stat.S_IMODE(route.stat().st_mode) == 0o640
    assert stat.S_IMODE((evidence / "dev-route-installed.yml").stat().st_mode) == 0o600
    assert not list(dynamic.glob(".iacp-dev-route-*"))

    second_evidence = go_live._new_evidence_dir(tmp_path / "idempotent-evidence")
    already_active = go_live._install_dev_route(route, production_route, template, second_evidence)
    assert already_active["changed"] is False
    assert already_active["already_active"] is True


def test_development_route_refuses_to_replace_operator_configuration(tmp_path):
    dynamic = tmp_path / "dynamic"
    dynamic.mkdir()
    production_route = dynamic / "iacp.yml"
    production_route.write_text("http: {}\n", encoding="utf-8")
    route = dynamic / "dev-iac.yml"
    original = "http:\n  routers:\n    operator-route: {}\n"
    route.write_text(original, encoding="utf-8")
    evidence = go_live._new_evidence_dir(tmp_path / "evidence")

    with pytest.raises(go_live.GateError, match="contenido distinto"):
        go_live._install_dev_route(
            route,
            production_route,
            go_live.ROOT / "deploy/portainer/traefik-dev.yml",
            evidence,
        )

    assert route.read_text(encoding="utf-8") == original


def test_development_route_removal_only_removes_managed_content(tmp_path):
    dynamic = tmp_path / "dynamic"
    dynamic.mkdir()
    route = dynamic / "dev-iac.yml"
    template = go_live.ROOT / "deploy/portainer/traefik-dev.yml"
    route.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    evidence = go_live._new_evidence_dir(tmp_path / "evidence")

    result = go_live._remove_dev_route(route, template, evidence)

    assert result["changed"] is True
    assert not route.exists()
    assert (evidence / "dev-route-before-removal.yml").read_text(encoding="utf-8") == template.read_text(encoding="utf-8")
    assert stat.S_IMODE((evidence / "dev-route-before-removal.yml").stat().st_mode) == 0o600


def test_development_activation_removes_new_route_when_external_readiness_fails(tmp_path, monkeypatch):
    dynamic = tmp_path / "dynamic"
    dynamic.mkdir()
    production_route = dynamic / "iacp.yml"
    production_route.write_text(
        "http:\n  services:\n    iacp-api:\n      loadBalancer:\n        servers:\n          - url: http://api:8000\n",
        encoding="utf-8",
    )
    route = dynamic / "dev-iac.yml"
    template = go_live.ROOT / "deploy/portainer/traefik-dev.yml"
    certificate = tmp_path / "fullchain.pem"
    evidence = go_live._new_evidence_dir(tmp_path / "evidence")
    manifest = {
        "stage": "develop",
        "domain": "dev-cyberfuel.com",
        "service_alias": "iacp-controlplane-api-dev",
        "dynamic_config": "/etc/iacp/tls/dynamic/dev-iac.yml",
        "tls_certificate_file": "/etc/iacp/tls/certs/fullchain.pem",
        "route_template": "deploy/portainer/traefik-dev.yml",
        "image_tag": IMAGE_TAG,
    }
    monkeypatch.setattr(go_live, "_develop_route_paths", lambda *_: (route, production_route, template, certificate))
    monkeypatch.setattr(go_live.os, "geteuid", lambda: 0)
    monkeypatch.setattr(go_live, "_require_tls_hostname", lambda *_: None)

    def unavailable(*args, **kwargs):
        raise go_live.GateError("El dominio no devolvió readiness")

    monkeypatch.setattr(go_live, "_check_external", unavailable)
    with pytest.raises(go_live.GateError, match="se retiró automáticamente"):
        go_live._activate_develop(manifest, evidence)

    assert not route.exists()
    assert (evidence / "dev-route-before-removal.yml").is_file()


def test_development_route_content_is_bound_to_the_dev_service(tmp_path):
    template = tmp_path / "route.yml"
    approved = go_live.ROOT / "deploy/portainer/traefik-dev.yml"
    template.write_text(
        approved.read_text(encoding="utf-8").replace(
            "http://iacp-controlplane-api-dev:8080", "http://api:8000"
        ),
        encoding="utf-8",
    )
    with pytest.raises(go_live.GateError, match="alias aprobado"):
        go_live._dev_route_content(template)


def test_tls_wildcards_cover_only_one_subdomain_label():
    assert go_live._dns_pattern_matches("*.cyberfuel.com", "api.cyberfuel.com")
    assert go_live._dns_pattern_matches("iac.cyberfuel.com", "iac.cyberfuel.com")
    assert not go_live._dns_pattern_matches("*.cyberfuel.com", "dev-cyberfuel.com")
    assert not go_live._dns_pattern_matches("*.iac.cyberfuel.com", "dev-cyberfuel.com")
    assert not go_live._dns_pattern_matches("*.cyberfuel.com", "dev.iac.cyberfuel.com")


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


def test_preloaded_images_verifies_each_service_and_digest(monkeypatch):
    config = {
        "services": {
            "api": {"image": f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"},
            **{
                f"worker-{number}": {"image": f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"}
                for number in range(1, 7)
            },
            "postgres": {"image": "postgres:17.6"},
        }
    }
    calls = []

    def inspect(command, *, label, timeout=120):
        calls.append((command, label, timeout))
        return "sha256:" + "b" * 64 + "\n"

    monkeypatch.setattr(go_live, "_run", inspect)
    verified = go_live._verify_preloaded_images(config)

    assert [item["service"] for item in verified] == [
        "api", "postgres", *(f"worker-{number}" for number in range(1, 7))
    ]
    assert len(calls) == 8
    assert all(call[0][:3] == ["docker", "image", "inspect"] for call in calls)


def test_preloaded_images_fails_when_a_service_image_is_missing(monkeypatch):
    config = {"services": {"api": {"image": f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"}}}

    def missing(*_args, **_kwargs):
        raise go_live.GateError("La imagen local no existe")

    monkeypatch.setattr(go_live, "_run", missing)
    with pytest.raises(go_live.GateError, match="imagen local no existe"):
        go_live._verify_preloaded_images(config)
