import importlib.util
import os
from pathlib import Path
import json
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_development_secrets_are_private_and_survive_repeated_initialization(tmp_path):
    initialize = module("init_dev_secrets").initialize
    path = tmp_path / "dev-secrets"
    initialize(path, os.getuid(), os.getgid())
    before = {file.name: file.read_bytes() for file in path.iterdir()}
    assert set(before) == {"api_token", "db_password"}
    assert all(len(content.strip()) == 64 for content in before.values())
    assert before["api_token"] != before["db_password"]
    assert path.stat().st_mode & 0o777 == 0o700
    assert all(file.stat().st_mode & 0o777 == 0o400 for file in path.iterdir())
    initialize(path, os.getuid(), os.getgid())
    assert {file.name: file.read_bytes() for file in path.iterdir()} == before


def test_invalid_existing_development_secret_is_never_replaced(tmp_path):
    path = tmp_path / "dev-secrets"
    path.mkdir()
    secret = path / "db_password"
    secret.write_text("invalid")
    with pytest.raises(RuntimeError, match="no se reemplazó"):
        module("init_dev_secrets").initialize(path, os.getuid(), os.getgid())
    assert secret.read_text() == "invalid"


@pytest.mark.parametrize("key,value", [
    ("IACP_REHEARSAL", "0"), ("DATABASE_HOST", "db"),
    ("POSTGRES_DB", "iacp"), ("CELERY_BROKER_URL", "redis://redis:6379/0"),
])
def test_rehearsal_refuses_runtime_settings(monkeypatch, key, value):
    for name, setting in {
        "IACP_REHEARSAL": "1", "DATABASE_HOST": "dev-db",
        "POSTGRES_DB": "iacp_dev", "CELERY_BROKER_URL": "redis://dev-redis:6379/0",
    }.items():
        monkeypatch.setenv(name, setting)
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="proyecto de desarrollo"):
        module("rehearse_transition").guard()


def test_openapi_exports_from_another_working_directory_without_host_dependencies(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/export-openapi.py"), "--stdout"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT / "app")},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads((ROOT / "docs/openapi.json").read_text())


def test_runtime_check_requires_https_before_instance_admission(monkeypatch, tmp_path):
    check_runtime = module("check_runtime")
    token_file = tmp_path / "api_token"
    token_file.write_text("test-token\n")
    monkeypatch.setenv("IACP_API_TOKEN_FILE", str(token_file))
    calls = []

    def request(_base_url, path, token=None, method="GET", body=None, extra_headers=None):
        calls.append((path, token, method, body, extra_headers))
        if path == "/health/ready":
            return 200, {"status": "ready"}
        if path == "/health/live":
            return 200, {"status": "live"}
        if path == "/v1/jobs/smoke" and method == "POST" and token is None:
            return 401, {"detail": "Unauthorized"}
        if path == "/v1/jobs/smoke" and method == "POST":
            return 202, {"job_id": "job-1"}
        if path == "/v1/jobs/job-1":
            return 200, {"status": "completed"}
        if path == "/v1/instances" and method == "POST":
            assert body["plan_id"] == "kvm-1"
            assert body["image_id"] == "ubuntu-2404"
            assert body["network"] == {"profile_id": "lab-dhcp"}
            return 422, {"detail": "ROOT_ACCESS_REQUIRES_HTTPS"}
        if path == "/openapi.json":
            return 200, {"paths": {"/v1/jobs/smoke": {}}}
        raise AssertionError(f"Unexpected request: {path} {method}")

    monkeypatch.setattr(check_runtime, "request", request)
    result = check_runtime.check("http://dev", timeout=1)

    assert result["status"] == "passed"
    assert result["provisioning"] == "https-required"
    assert not any(path.startswith("/v1/instances/") for path, *_ in calls)
    assert not any(path.startswith("/v1/operations/") for path, *_ in calls)
