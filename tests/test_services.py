import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from api.db import database
from api.main import app
from api.service_secrets import decrypt
from worker import services
from worker.service_recipes import render, RecipeError


@pytest.mark.no_database
def test_service_execution_timeout_allows_slow_wan_downloads(monkeypatch):
    from worker.service_executor import service_execution_timeout_seconds

    monkeypatch.delenv("IACP_SERVICE_EXECUTION_TIMEOUT", raising=False)
    assert service_execution_timeout_seconds() == 10800
    assert service_execution_timeout_seconds("3600") == 3600
    assert service_execution_timeout_seconds("invalid") == 10800
    assert service_execution_timeout_seconds("999999") == 21600
    assert service_execution_timeout_seconds("30") == 10800


@pytest.mark.no_database
def test_ubuntu_apt_sources_are_upgraded_to_https_before_install():
    import re

    from worker.service_executor import ubuntu_https_sources_task

    task = ubuntu_https_sources_task()
    replace = task["ansible.builtin.replace"]
    assert replace["path"] == "/etc/apt/sources.list.d/ubuntu.sources"
    source = (
        "URIs: http://archive.ubuntu.com/ubuntu\n"
        "URIs: http://security.ubuntu.com/ubuntu\n"
        "URIs: http://packages.example.invalid/repo\n"
    )
    assert re.sub(replace["regexp"], replace["replace"], source) == (
        "URIs: https://archive.ubuntu.com/ubuntu\n"
        "URIs: https://security.ubuntu.com/ubuntu\n"
        "URIs: http://packages.example.invalid/repo\n"
    )


@pytest.mark.no_database
def test_service_egress_preflight_checks_verified_https_endpoints_with_bounded_wait():
    from worker.service_executor import (
        SERVICE_EGRESS_PREFLIGHT_SCRIPT,
        service_egress_preflight_task,
    )

    compile(SERVICE_EGRESS_PREFLIGHT_SCRIPT, "service-egress-preflight", "exec")
    assert "archive.ubuntu.com" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "/ubuntu/dists/noble/InRelease" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "security.ubuntu.com" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "/ubuntu/dists/noble-security/InRelease" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "registry-1.docker.io" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert '"/v2/", True' in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "ssl.create_default_context()" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "subprocess.run(" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "timeout=DNS_TIMEOUT_SECONDS" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "DNS_TIMEOUT_SECONDS = 3" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "MAX_ATTEMPTS = 3" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "random.uniform(0, 0.5)" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert (
        "socket.create_connection((address, 443), timeout=5)"
        in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    )
    assert "server_hostname=host" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "http.client.HTTPResponse(connection)" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert 'return None, "DNS_TIMEOUT"' in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert 'return "ROUTE"' in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert 'return "TLS"' in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert 'return None, "DNS"' in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "challenge.lower().startswith(\"bearer \")" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    assert "IACP_SERVICE_EGRESS_PREFLIGHT_FAILED:" in SERVICE_EGRESS_PREFLIGHT_SCRIPT
    task = service_egress_preflight_task()
    assert task["name"] == "Validar DNS y egress HTTPS antes de instalar dependencias"
    assert task["ansible.builtin.command"]["argv"] == [
        "timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "90s",
        "python3",
        "-c",
        SERVICE_EGRESS_PREFLIGHT_SCRIPT,
    ]
    assert task["changed_when"] is False


@pytest.mark.no_database
def test_service_egress_preflight_stops_when_dns_lookup_hangs():
    import sys
    import time

    from worker.service_executor import SERVICE_EGRESS_PREFLIGHT_SCRIPT

    script = SERVICE_EGRESS_PREFLIGHT_SCRIPT.replace(
        "DNS_TIMEOUT_SECONDS = 3", "DNS_TIMEOUT_SECONDS = 0.05"
    ).replace("MAX_ATTEMPTS = 3", "MAX_ATTEMPTS = 1")
    script = script.replace(
        "import subprocess\n",
        "import subprocess\n"
        "_real_subprocess_run = subprocess.run\n"
        "def _test_subprocess_run(arguments, **options):\n"
        "    if arguments and arguments[0] == 'getent':\n"
        "        arguments = ['/usr/bin/sleep', '10']\n"
        "    return _real_subprocess_run(arguments, **options)\n"
        "subprocess.run = _test_subprocess_run\n",
        1,
    )

    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode != 0
    assert "archive.ubuntu.com:DNS_TIMEOUT" in result.stderr
    assert "security.ubuntu.com:DNS_TIMEOUT" in result.stderr
    assert "registry-1.docker.io:DNS_TIMEOUT" in result.stderr
    assert time.monotonic() - started < 3


@pytest.mark.no_database
def test_nextcloud_install_wait_handles_transient_non_json_status(tmp_path):
    from worker.service_executor import NEXTCLOUD_READY_SCRIPT

    state = tmp_path / "attempt"
    state.write_text("0")
    setup = f"""
php() {{
  IFS= read -r status_calls < "$IACP_TEST_ATTEMPT"
  case "$status_calls" in
    0) printf 'Nextcloud setup in progress\\n' ;;
    1) printf '{{"installed": false}}\\n' ;;
    *) printf '{{ "installed" : true }}\\n' ;;
  esac
  status_calls=$((status_calls + 1))
  printf '%s' "$status_calls" > "$IACP_TEST_ATTEMPT"
}}
sleep() {{ :; }}
"""
    environment = os.environ | {"IACP_TEST_ATTEMPT": str(state)}

    result = subprocess.run(
        ["sh", "-ec", setup + NEXTCLOUD_READY_SCRIPT],
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == "IACP_NEXTCLOUD_INSTALLED\n"
    assert state.read_text() == "3"


@pytest.mark.no_database
def test_nextcloud_install_wait_allows_more_than_sixty_readiness_checks(tmp_path):
    from worker.service_executor import (
        NEXTCLOUD_READY_ATTEMPTS,
        NEXTCLOUD_READY_POLL_SECONDS,
        NEXTCLOUD_READY_SCRIPT,
    )

    state = tmp_path / "attempt"
    state.write_text("0")
    setup = f"""
php() {{
  IFS= read -r status_calls < "$IACP_TEST_ATTEMPT"
  if [ "$status_calls" -lt 65 ]; then
    printf '{{"installed": false}}\\n'
  else
    printf '{{"installed": true}}\\n'
  fi
  status_calls=$((status_calls + 1))
  printf '%s' "$status_calls" > "$IACP_TEST_ATTEMPT"
}}
sleep() {{ [ "$1" = "{NEXTCLOUD_READY_POLL_SECONDS}" ]; }}
"""
    result = subprocess.run(
        ["sh", "-ec", setup + NEXTCLOUD_READY_SCRIPT],
        capture_output=True,
        text=True,
        env=os.environ | {"IACP_TEST_ATTEMPT": str(state)},
        timeout=10,
    )

    assert NEXTCLOUD_READY_ATTEMPTS == 240
    assert result.returncode == 0
    assert result.stdout == "IACP_NEXTCLOUD_INSTALLED\n"
    assert state.read_text() == "66"


def body(name="service-demo"):
    return {
        "application_id": "nextcloud",
        "instance": {
            "name": name,
            "plan_id": "kvm-2",
            "image_id": "ubuntu-2404",
            "access": {
                "root_password": "password-ficticio-123",
                "ssh_public_keys": ["ssh-ed25519 AAAAexample"],
            },
        },
        "configuration": {"domain": "cloud.example.org"},
    }


@pytest.fixture
def client(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("IACP_SERVICE_KEY_FILE", str(key))
    monkeypatch.setenv("IACP_SERVICES_ENABLED", "1")
    monkeypatch.setenv("IACP_SERVICE_MAX_CONCURRENT", "2")
    with TestClient(app) as c:
        yield c


def create(client, name="service-demo", key="create-service-1"):
    response = client.post(
        "/v1/service-instances", json=body(name), headers={"Idempotency-Key": key}
    )
    assert response.status_code == 202, response.text
    return response.json()


def ready(instance_id):
    with database() as db:
        db.execute(
            "UPDATE instances SET status='ready', power_state='running', vmid=40001 WHERE instance_id=?",
            (instance_id,),
        )
        db.execute(
            "UPDATE operations SET status='succeeded' WHERE instance_id=?",
            (instance_id,),
        )


def test_disabled_catalog_and_no_side_effects(client, monkeypatch):
    monkeypatch.setenv("IACP_SERVICES_ENABLED", "0")
    assert all(not a["enabled"] for a in client.get("/v1/applications").json())
    assert client.post("/v1/service-instances", json=body()).status_code == 503
    with database() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM instances").fetchone()["n"] == 0


def test_services_catalog_selects_only_ubuntu_2404(client):
    catalog = {item["application_id"]: item for item in client.get("/v1/applications").json()}
    assert set(catalog) == {"nextcloud", "n8n", "hermes", "openclaw"}
    for application in catalog:
        assert catalog[application]["image_id"] == "ubuntu-2404"
        assert catalog[application]["supported_image_ids"] == ["ubuntu-2404"]


def test_atomic_idempotent_order_and_encrypted_credentials(client):
    order = create(client)
    assert create(client) == order
    with database() as db:
        service = db.execute("SELECT * FROM service_instances").fetchone()
        secrets = decrypt(service["secrets_encrypted"])
        operation = db.execute("SELECT payload_json FROM operations").fetchone()[
            "payload_json"
        ]
        assert secrets["ssh_private_key"] not in operation
        assert secrets["admin_password"] not in service["secrets_encrypted"]
        assert (
            secrets["ssh_public_key"]
            in json.loads(operation)["spec"]["ssh_public_keys"]
        )
    assert services.claim("service-1") is None
    ready(order["instance_id"])
    claimed = services.claim("service-1")
    assert claimed["job_id"] == order["job_id"]
    assert services.claim("service-2") is None
    assert (
        client.post(
            f"/v1/instances/{order['instance_id']}/power/shutdown",
            headers={"Idempotency-Key": "shutdown-1"},
        ).status_code
        == 409
    )


def test_password_only_service_injects_only_an_internal_temporary_key(client):
    request = body("service-password-only")
    request["instance"]["access"]["ssh_public_keys"] = []
    response = client.post(
        "/v1/service-instances",
        json=request,
        headers={"Idempotency-Key": "password-only-service"},
    )
    assert response.status_code == 202, response.text
    with database() as db:
        operation = json.loads(
            db.execute("SELECT payload_json FROM operations").fetchone()["payload_json"]
        )
        service = db.execute("SELECT * FROM service_instances").fetchone()
        secrets = decrypt(service["secrets_encrypted"])
    assert operation["spec"]["ssh_public_keys"] == [secrets["ssh_public_key"]]
    assert "ssh-ed25519 AAAAexample" not in operation["spec"]["ssh_public_keys"]
    assert secrets["ssh_private_key"] not in json.dumps(operation)


def test_password_only_service_requires_a_root_password(client):
    request = body("service-without-access")
    request["instance"]["access"] = {"ssh_public_keys": []}
    response = client.post("/v1/service-instances", json=request)
    assert response.status_code == 422
    assert "root_password" in response.text


def test_service_url_is_hidden_until_application_installation_finishes(client):
    request = body("service-url-readiness")
    request["configuration"] = {"access_mode": "ip"}
    order = client.post(
        "/v1/service-instances",
        json=request,
        headers={"Idempotency-Key": "service-url-readiness"},
    ).json()
    with database() as db:
        db.execute(
            "INSERT INTO service_bindings VALUES (?, ?, ?)",
            (
                order["service_id"],
                json.dumps({"address": "192.0.2.42", "host_key": "ssh-ed25519 AAAA"}),
                "2026-11-17T00:00:00+00:00",
            ),
        )
    assert client.get(f"/v1/service-instances/{order['service_id']}").json()["access_url"] is None
    with database() as db:
        db.execute(
            "UPDATE service_instances SET status='awaiting_customer' WHERE service_id=?",
            (order["service_id"],),
        )
    assert (
        client.get(f"/v1/service-instances/{order['service_id']}").json()["access_url"]
        == "https://192.0.2.42/"
    )


def test_pool_claims_and_blocked_execution(client):
    orders = [create(client, f"app-{i}", f"create-{i}") for i in range(3)]
    for order in orders:
        ready(order["instance_id"])
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(services.claim, ["a", "b", "c"]))
    assert sum(c is not None for c in claims) == 2
    job = next(c for c in claims if c)
    services.process(job["worker_id"], job)
    assert client.get(f"/v1/service-jobs/{job['job_id']}").json()["status"] == "blocked"


def test_service_execution_failure_keeps_only_safe_diagnostic(client, monkeypatch):
    from worker.service_diagnostics import ServiceExecutionUncertain

    order = create(client)
    ready(order["instance_id"])
    job = services.claim("diagnostic-worker")

    def fail(*_args):
        raise ServiceExecutionUncertain(
            "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_FAILED_NEXTCLOUD_INSTALL"
        )

    monkeypatch.setattr(services, "execute", fail)
    services.process("diagnostic-worker", job)
    result = client.get(f"/v1/service-jobs/{job['job_id']}")
    assert result.json()["status"] == "uncertain"
    assert result.json()["error"] == (
        "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_FAILED_NEXTCLOUD_INSTALL"
    )


def test_application_image_pull_failure_is_retryable(client, monkeypatch):
    from worker.service_diagnostics import ServiceExecutionFailed

    order = create(client)
    ready(order["instance_id"])
    job = services.claim("image-pull-worker")

    def fail(*_args):
        raise ServiceExecutionFailed(
            "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_APPLICATION_IMAGE_PULL"
        )

    monkeypatch.setattr(services, "execute", fail)
    services.process("image-pull-worker", job)
    result = client.get(f"/v1/service-jobs/{job['job_id']}")
    assert result.json()["status"] == "failed"
    assert result.json()["error"] == (
        "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_APPLICATION_IMAGE_PULL"
    )


@pytest.mark.no_database
def test_safe_ansible_error_parser_never_returns_raw_output():
    from worker.service_diagnostics import safe_execution_error

    error = safe_execution_error(
        "IACP_TASK:APPLICATION_START\n"
        "IACP_FAILED:NEXTCLOUD_INSTALL\n"
        "token=private-value\n",
        return_code=2,
    )
    assert error == "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_FAILED_NEXTCLOUD_INSTALL"
    assert "private-value" not in error


@pytest.mark.no_database
def test_safe_ansible_timeout_reports_last_allowlisted_task():
    from worker.service_diagnostics import safe_execution_error

    error = safe_execution_error(
        "IACP_TASK:DOCKER_DEPENDENCIES\n", return_code=-15, timed_out=True
    )
    assert error == "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_TIMEOUT_DOCKER_DEPENDENCIES"


@pytest.mark.no_database
def test_safe_image_pull_failure_is_retryable_without_marking_uncertain():
    from worker.service_diagnostics import safe_execution_error

    error = safe_execution_error(
        "IACP_TASK:APPLICATION_IMAGE_PULL\n"
        "IACP_FAILED:APPLICATION_IMAGE_PULL\n"
        "token=private-value\n",
        return_code=2,
    )
    assert error == "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_APPLICATION_IMAGE_PULL"
    assert "private-value" not in error


@pytest.mark.no_database
def test_safe_egress_preflight_failure_is_classified_before_mutating_service():
    from worker.service_diagnostics import safe_execution_error, task_code

    task_name = "Validar DNS y egress HTTPS antes de instalar dependencias"
    assert task_code(task_name) == "SERVICE_EGRESS_PREFLIGHT"
    error = safe_execution_error(
        "IACP_TASK:SERVICE_EGRESS_PREFLIGHT\n"
        "IACP_FAILED:SERVICE_EGRESS_PREFLIGHT\n"
        "DNS server details are redacted\n",
        return_code=2,
    )
    assert error == "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_SERVICE_EGRESS_PREFLIGHT"
    assert "DNS server details" not in error


@pytest.mark.no_database
@pytest.mark.parametrize("result_event", ["FAILED", "UNREACHABLE"])
def test_initial_ssh_wait_failure_is_safe_to_retry_without_remote_changes(result_event):
    from worker.service_diagnostics import safe_execution_error

    error = safe_execution_error(
        "IACP_TASK:SSH_CONNECTIVITY\n"
        f"IACP_{result_event}:SSH_CONNECTIVITY\n"
        "token=private-value\n",
        return_code=2,
    )
    assert error == "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_SSH_CONNECTIVITY"
    assert "private-value" not in error


def test_ansible_safe_callback_reports_redacted_failure_event(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from worker.service_diagnostics import safe_execution_error

    callback_dir = Path(__file__).parents[1] / "worker" / "ansible_callbacks"
    playbook = tmp_path / "failed-playbook.json"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tasks": [
                        {
                            "name": "Instalar dependencias de Ubuntu 24.04",
                            "ansible.builtin.command": {
                                "argv": [
                                    "/bin/sh",
                                    "-c",
                                    "echo token-de-prueba; exit 1",
                                ]
                            },
                            "no_log": True,
                        }
                    ],
                }
            ]
        )
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible"),
        "ANSIBLE_CALLBACK_PLUGINS": str(callback_dir),
        "ANSIBLE_STDOUT_CALLBACK": "iacp_safe_summary",
        "ANSIBLE_LOAD_CALLBACK_PLUGINS": "1",
        "ANSIBLE_NOCOLOR": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ansible.cli.playbook",
            "-i",
            "localhost,",
            str(playbook),
        ],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    lines = result.stdout.splitlines()
    assert lines[0] == "IACP_TASK:DOCKER_DEPENDENCIES"
    assert len(lines) == 2
    assert lines[1] in {
        "IACP_FAILED:DOCKER_DEPENDENCIES",
        "IACP_UNREACHABLE:DOCKER_DEPENDENCIES",
    }
    assert "token-de-prueba" not in result.stdout + result.stderr
    event_type = "UNREACHABLE" if "UNREACHABLE" in lines[1] else "FAILED"
    assert safe_execution_error(result.stdout, return_code=result.returncode) == (
        f"SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_{event_type}_DOCKER_DEPENDENCIES"
    )


def test_recovery_requires_explicit_stop_and_generation_changes(client):
    order = create(client)
    ready(order["instance_id"])
    job = services.claim("lost")
    with database() as db:
        db.execute("UPDATE service_jobs SET last_heartbeat='2000-01-01T00:00:00+00:00'")
    services.recover()
    path = f"/v1/service-jobs/{order['job_id']}/retry"
    assert (
        client.post(path, json={}, headers={"Idempotency-Key": "retry-1"}).status_code
        == 409
    )
    assert (
        client.post(
            path,
            json={"previous_execution_stopped": True},
            headers={"Idempotency-Key": "retry-2"},
        ).status_code
        == 202
    )
    claimed = services.claim("replacement")
    assert claimed["generation"] == job["generation"] + 1
    with pytest.raises(RuntimeError):
        services.heartbeat("lost", job)


def test_delivery_one_time_and_secret_removal(client, monkeypatch):
    order = create(client)
    ready(order["instance_id"])
    monkeypatch.setattr(services, "execute", lambda *_: None)
    job = services.claim("a")
    services.process("a", job)
    path = f"/v1/service-instances/{order['service_id']}"
    assert (
        client.post(
            path + "/deliver", headers={"Idempotency-Key": "deliver-1"}
        ).status_code
        == 409
    )
    credentials = client.post(path + "/credentials")
    assert credentials.status_code == 200
    assert credentials.headers["cache-control"] == "no-store"
    assert client.post(path + "/credentials").status_code == 409
    assert (
        client.post(
            path + "/deliver", headers={"Idempotency-Key": "deliver-2"}
        ).status_code
        == 202
    )
    services.process("b", services.claim("b"))
    with database() as db:
        assert (
            db.execute("SELECT secrets_encrypted FROM service_instances").fetchone()[
                "secrets_encrypted"
            ]
            is None
        )


def test_credential_receipt_recovers_disclosure_until_delivery(client, monkeypatch):
    order = create(client)
    ready(order["instance_id"])
    monkeypatch.setattr(services, "execute", lambda *_: None)
    services.process("a", services.claim("a"))
    path = f"/v1/service-instances/{order['service_id']}"
    disclosure = {"Idempotency-Key": "credential-disclosure-1"}
    received = client.post(path + "/credentials", headers=disclosure)
    assert received.status_code == 200
    assert client.post(path + "/credentials", headers=disclosure).json() == received.json()
    assert client.post(path + "/credentials", headers={"Idempotency-Key": "another-attempt"}).status_code == 409
    detail = client.get(path).json()
    assert detail["credential_receipt_required"] is True
    assert detail["credentials_recovery_possible"] is True
    assert received.json()["credential_receipt"] not in json.dumps(detail)
    assert client.post(path + "/deliver", headers={"Idempotency-Key": "handoff-1"}).status_code == 409
    queued = client.post(
        path + "/deliver",
        headers={
            "Idempotency-Key": "handoff-2",
            "X-IACP-Credential-Receipt": received.json()["credential_receipt"],
        },
    )
    assert queued.status_code == 202
    services.process("b", services.claim("b"))
    assert client.post(path + "/credentials", headers=disclosure).status_code == 409
    with database() as db:
        assert db.execute("SELECT count(*) AS n FROM service_credential_retrievals").fetchone()["n"] == 0


def test_expired_disclosure_renews_only_for_original_key(client, monkeypatch):
    order = create(client)
    ready(order["instance_id"])
    monkeypatch.setattr(services, "execute", lambda *_: None)
    services.process("a", services.claim("a"))
    path = f"/v1/service-instances/{order['service_id']}"
    disclosure = {"Idempotency-Key": "credential-recovery"}
    first = client.post(path + "/credentials", headers=disclosure)
    assert first.status_code == 200
    with database() as db:
        db.execute(
            "UPDATE service_instances SET credentials_retrieved_at=? WHERE service_id=?",
            ("2000-01-01T00:00:00+00:00", order["service_id"]),
        )
    assert client.post(path + "/credentials", headers={"Idempotency-Key": "new-attempt"}).status_code == 409
    recovered = client.post(path + "/credentials", headers=disclosure)
    assert recovered.status_code == 200
    assert recovered.json()["credential_receipt"] != first.json()["credential_receipt"]


def test_cancel_order_after_definitive_failure_without_resources(client):
    order = create(client)
    with database() as db:
        db.execute("UPDATE operations SET status='failed' WHERE operation_id=?", (order["operation_id"],))
        db.execute("UPDATE instances SET status='failed' WHERE instance_id=?", (order["instance_id"],))
    response = client.post(f"/v1/service-jobs/{order['job_id']}/cancel")
    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"


def test_cancel_queued_order_cancels_unused_provision_and_clears_credentials(client):
    order = create(client)
    from pathlib import Path

    with database() as db:
        row = db.execute("SELECT secret_path FROM operations WHERE operation_id=?", (order["operation_id"],)).fetchone()
        operation_secret = Path(row["secret_path"])
    assert operation_secret.is_file()
    response = client.post(f"/v1/service-jobs/{order['job_id']}/cancel")
    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"
    assert not operation_secret.exists()
    with database() as db:
        row = db.execute("SELECT status, secret_path FROM operations WHERE operation_id=?", (order["operation_id"],)).fetchone()
        service = db.execute("SELECT secrets_encrypted FROM service_instances WHERE service_id=?", (order["service_id"],)).fetchone()
    assert dict(row) == {"status": "failed", "secret_path": None}
    assert service["secrets_encrypted"] is None


def test_cancellation_requires_verified_cleanup_after_provision(client, monkeypatch):
    order = create(client)
    ready(order["instance_id"])
    cancelled = client.post(f"/v1/service-jobs/{order['job_id']}/cancel")
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "queued"
    path = f"/v1/service-instances/{order['service_id']}"
    assert client.get(path).json()["status"] == "cancelling"
    job = services.claim("cleanup")
    assert job["action"] == "cleanup"
    assert job["job_id"] == cancelled.json()["job_id"]
    monkeypatch.setattr(services, "execute", lambda *_: None)
    services.process("cleanup", job)
    detail = client.get(path).json()
    assert detail["status"] == "cancelled"
    assert detail["infrastructure_status"] == "ready"


def test_superseded_service_job_cannot_be_retried_or_cancelled(client):
    order = create(client)
    with database() as db:
        db.execute("UPDATE service_jobs SET status='blocked' WHERE job_id=?", (order["job_id"],))
        db.execute(
            "INSERT INTO service_jobs(job_id, service_id, status, created_at) VALUES (?, ?, 'queued', ?)",
            ("current-job", order["service_id"], "2099-01-01T00:00:00+00:00"),
        )
    path = f"/v1/service-jobs/{order['job_id']}"
    for action in ("retry", "cancel"):
        response = client.post(f"{path}/{action}", json={})
        assert response.status_code == 409
        assert response.json()["detail"] == "SERVICE_JOB_SUPERSEDED"
    assert client.get("/v1/service-jobs/current-job").json()["status"] == "queued"


def test_render_pins_no_agent_public_ports():
    with pytest.raises(RecipeError):
        render("hermes", {}, {}, {"images": {"app": "hermes:latest"}})
    compose, _ = render(
        "hermes",
        {"model": "test"},
        {"provider_api_key": "s$ecret"},
        {"images": {"app": "example/hermes@sha256:" + "a" * 64}},
    )
    assert "ports" not in compose["services"]["app"]
    assert compose["services"]["app"]["environment"]["OPENROUTER_API_KEY"] == "s$$ecret"


def test_conflicting_idempotency_and_cancel_blocks_claim(client):
    order = create(client)
    changed = body()
    changed["configuration"]["domain"] = "other.example.org"
    assert (
        client.post(
            "/v1/service-instances",
            json=changed,
            headers={"Idempotency-Key": "create-service-1"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/v1/service-jobs/{order['job_id']}/cancel",
            headers={"Idempotency-Key": "cancel-1"},
        ).status_code
        == 202
    )
    ready(order["instance_id"])
    assert services.claim("a") is None


def test_ansible_playbook_syntax_for_all_recipes(monkeypatch, tmp_path):
    import base64
    import subprocess
    from api.service_secrets import installation_credentials, encrypt
    from worker import service_executor
    from api.db import initialize

    initialize()

    monkeypatch.setattr(service_executor, "pin_release", lambda *_: None)

    monkeypatch.setenv("IACP_SERVICE_EXECUTION_ENABLED", "1")
    import socket

    original_resolver = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: (
            [(2, 1, 6, "", ("198.51.100.2", 443))]
            if host == "cloud.example.org"
            else original_resolver(host, *args, **kwargs)
        ),
    )
    master = tmp_path / "key"
    master.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("IACP_SERVICE_KEY_FILE", str(master))
    bindings = tmp_path / "bindings"
    bindings.mkdir()
    (bindings / "instance-1.json").write_text(
        json.dumps(
            {
                "instance_id": "instance-1",
                "vmid": 40001,
                "address": "198.51.100.2",
                "domain": "cloud.example.org",
                "host_key": "ssh-ed25519 AAAAexample",
            }
        )
    )
    monkeypatch.setenv("IACP_SERVICE_BINDINGS_DIR", str(bindings))
    releases_file = tmp_path / "releases.json"
    monkeypatch.setenv("IACP_SERVICE_RELEASES_FILE", str(releases_file))
    real_run = subprocess.run
    original = subprocess.Popen

    class SyntaxProcess:
        returncode = 0

        def __init__(self, args, **kwargs):
            from pathlib import Path
            control = Path(kwargs["env"]["ANSIBLE_SSH_CONTROL_PATH_DIR"])
            callback_dir = Path(kwargs["env"]["ANSIBLE_CALLBACK_PLUGINS"])
            assert control.parent == Path(kwargs["env"]["ANSIBLE_LOCAL_TEMP"]).parent
            assert control.parent.is_dir()
            assert callback_dir == (
                Path(service_executor.__file__).resolve().parent
                / "ansible_callbacks"
            )
            assert kwargs["env"]["ANSIBLE_STDOUT_CALLBACK"] == "iacp_safe_summary"
            assert kwargs["env"]["ANSIBLE_LOAD_CALLBACK_PLUGINS"] == "1"
            playbook = json.loads((control.parent / "playbook.json").read_text())
            assert playbook[0]["gather_facts"] is False
            assert playbook[0]["pre_tasks"] == [
                {
                    "name": "Esperar conectividad SSH",
                    "ansible.builtin.wait_for_connection": {
                        "connect_timeout": 15,
                        "delay": 5,
                        "sleep": 5,
                        "timeout": 600,
                    },
                },
                {
                    "name": "Recopilar facts del invitado",
                    "ansible.builtin.setup": {"gather_subset": ["min"]},
                },
            ]
            service_tasks = playbook[0]["tasks"][1]["block"]
            if application == "nextcloud" and expected_action == "install":
                compose = json.loads((control.parent / "payload/compose.yaml").read_text())
                admin_environment = compose["services"]["app"]["environment"]
                assert admin_environment["NEXTCLOUD_ADMIN_USER"] == "cliente_admin"
                assert admin_environment["NEXTCLOUD_ADMIN_PASSWORD"] == "nextcloud-local-test-password"
                config_copy = next(
                    task for task in service_tasks if task.get("name") == "Escribir configuración"
                )
                assert config_copy["no_log"] is True
            if expected_action == "install":
                assert any(
                    task.get("name") == "Validar Ubuntu Server 24.04"
                    and task.get("ansible.builtin.assert", {}).get("that")
                    == [
                        "ansible_facts.distribution == 'Ubuntu'",
                        "ansible_facts.distribution_version == '24.04'",
                    ]
                    for task in service_tasks
                )
                assert any(
                    task.get("name") == "Instalar dependencias de Ubuntu 24.04"
                    and task.get("ansible.builtin.apt", {}).get("name")
                    == ["docker.io", "docker-compose-v2"]
                    and task.get("ansible.builtin.apt", {}).get("lock_timeout") == 600
                    and task.get("ansible.builtin.apt", {}).get(
                        "update_cache_retries"
                    )
                    == 3
                    and task.get("retries") == 3
                    for task in service_tasks
                )
                assert any(
                    task.get("name") == "Usar HTTPS en los repositorios estándar de Ubuntu"
                    and task.get("ansible.builtin.replace", {}).get("path")
                    == "/etc/apt/sources.list.d/ubuntu.sources"
                    and "URIs: https://\\1.ubuntu.com/ubuntu"
                    == task.get("ansible.builtin.replace", {}).get("replace")
                    for task in service_tasks
                )
                https_sources = next(
                    task
                    for task in service_tasks
                    if task.get("name") == "Usar HTTPS en los repositorios estándar de Ubuntu"
                )
                package_install = next(
                    task
                    for task in service_tasks
                    if task.get("name") == "Instalar dependencias de Ubuntu 24.04"
                )
                egress_preflight = next(
                    task
                    for task in service_tasks
                    if task.get("name")
                    == "Validar DNS y egress HTTPS antes de instalar dependencias"
                )
                assert service_tasks.index(https_sources) < service_tasks.index(
                    egress_preflight
                )
                assert service_tasks.index(egress_preflight) < service_tasks.index(
                    package_install
                )
                egress_argv = egress_preflight["ansible.builtin.command"]["argv"]
                assert egress_argv[:6] == [
                    "timeout",
                    "--signal=TERM",
                    "--kill-after=5s",
                    "90s",
                    "python3",
                    "-c",
                ]
                assert "archive.ubuntu.com" in egress_argv[6]
                assert "registry-1.docker.io" in egress_argv[6]
                assert not any("AlmaLinux" in task.get("name", "") for task in service_tasks)
            else:
                task_names = [task.get("name") for task in service_tasks]
                assert task_names[0] == "Esperar cloud-init" and task_names[-1] == "Retirar clave temporal"
                if expected_action == "cleanup":
                    retirement = next(
                        task for task in service_tasks if task.get("name") == "Retirar contenedores de la aplicación"
                    )
                    assert retirement["ansible.builtin.command"]["argv"][-1] == "down"
                    assert retirement["when"] == "existing_compose.stat.exists"
                else:
                    assert len(task_names) == 2
            image_pull_tasks = [
                task
                for task in service_tasks
                if task.get("name") == "Descargar imágenes de aplicación"
            ]
            application_start_tasks = [
                task for task in service_tasks if task.get("name") == "Arrancar aplicación"
            ]
            if expected_action == "install":
                assert len(image_pull_tasks) == 1
                assert len(application_start_tasks) == 1
                assert service_tasks.index(image_pull_tasks[0]) < service_tasks.index(application_start_tasks[0])
                assert image_pull_tasks[0]["ansible.builtin.command"]["argv"][-1] == "pull"
                assert image_pull_tasks[0]["retries"] == 3
                assert image_pull_tasks[0]["delay"] == 10
                assert image_pull_tasks[0]["until"] == "application_image_pull.rc == 0"
            else:
                assert image_pull_tasks == []
                assert application_start_tasks == []
            trusted_domain_tasks = [
                task
                for task in service_tasks
                if task.get("name") == "Autorizar hostname de acceso de Nextcloud"
            ]
            trusted_domain_checks = [
                task
                for task in service_tasks
                if task.get("name") == "Verificar hostname confiable de Nextcloud"
            ]
            if application == "nextcloud" and expected_action == "install":
                assert len(trusted_domain_tasks) == 1
                assert trusted_domain_tasks[0]["no_log"] is True
                assert trusted_domain_tasks[0]["ansible.builtin.command"]["argv"][-4:] == [
                    "trusted_domains",
                    "1",
                    "--value",
                    "cloud.example.org",
                ]
                assert len(trusted_domain_checks) == 1
                assert trusted_domain_checks[0]["no_log"] is True
                assert trusted_domain_checks[0]["ansible.builtin.command"]["argv"][-2:] == [
                    "trusted_domains",
                    "1",
                ]
                assert "cloud.example.org" in trusted_domain_checks[0]["failed_when"]
            else:
                assert trusted_domain_tasks == []
                assert trusted_domain_checks == []
            owner_setup_tasks = [
                task
                for task in service_tasks
                if task.get("name") == "Configurar propietario inicial de n8n"
            ]
            if application == "n8n" and expected_action == "install":
                assert len(owner_setup_tasks) == 1
                proxy_stop = next(task for task in service_tasks if task.get("name") == "Cerrar proxy durante el alta de n8n")
                proxy_start = next(task for task in service_tasks if task.get("name") == "Publicar proxy tras verificar propietario de n8n")
                start = application_start_tasks[0]
                assert start["ansible.builtin.command"]["argv"][-2:] == ["app", "postgres"]
                assert proxy_stop["ansible.builtin.command"]["argv"][-2:] == ["stop", "caddy"]
                assert proxy_start["ansible.builtin.command"]["argv"][-1] == "caddy"
                assert service_tasks.index(proxy_stop) < service_tasks.index(start)
                assert service_tasks.index(start) < service_tasks.index(owner_setup_tasks[0])
                assert service_tasks.index(owner_setup_tasks[0]) < service_tasks.index(proxy_start)
                assert owner_setup_tasks[0]["no_log"] is True
                owner_command = owner_setup_tasks[0]["ansible.builtin.command"]
                assert "owner/setup" in owner_command["argv"][-1]
                assert "local-n8n-password" not in json.dumps(owner_command["argv"])
                owner_input = json.loads(
                    base64.b64decode(owner_command["stdin"])
                )
                assert owner_input["password"] == "local-n8n-password"
            else:
                assert owner_setup_tasks == []
            with monkeypatch.context() as context:
                context.setattr(subprocess, "Popen", original)
                result = real_run(
                    args + ["--syntax-check"],
                    env=kwargs["env"],
                    capture_output=True,
                    text=True,
                )
            assert result.returncode == 0, result.stderr

        def poll(self):
            return 0

    monkeypatch.setattr(service_executor.subprocess, "Popen", SyntaxProcess)

    components = {
        "nextcloud": ["app", "postgres", "redis", "caddy"],
        "n8n": ["app", "postgres", "caddy"],
        "hermes": ["app"],
        "openclaw": ["app"],
    }
    for application, images in components.items():
        releases_file.write_text(
            json.dumps(
                {
                    application: {
                        "approved": True,
                        "recipe_version": "1",
                        "verification_argv": ["true"],
                        "images": {
                            name: "example/" + name + "@sha256:" + "a" * 64
                            for name in images
                        },
                    }
                }
            )
        )
        secrets = installation_credentials() | {"provider_api_key": "test-key"}
        if application == "n8n":
            secrets["owner_password"] = "local-n8n-password"
        elif application == "nextcloud":
            secrets["admin_password"] = "nextcloud-local-test-password"
        service = {
            "service_id": "service-1",
            "instance_id": "instance-1",
            "vmid": 40001,
            "application_id": application,
            "recipe_version": "1",
            "spec_json": "{}",
            "configuration_json": json.dumps(
                {
                    "domain": "cloud.example.org",
                    "admin_user": "cliente_admin",
                    "owner_email": "owner@example.invalid",
                    "owner_first_name": "Prueba",
                    "owner_last_name": "Local",
                    "model": "test/model",
                }
            ),
            "secrets_encrypted": encrypt(secrets),
        }
        expected_action = "install"
        service_executor.execute(service, {"action": "install"}, lambda: None)
        expected_action = "handoff"
        service_executor.execute(service, {"action": "handoff"}, lambda: None)
        expected_action = "cleanup"
        service_executor.execute(service, {"action": "cleanup"}, lambda: None)


def test_six_infrastructure_slots_and_two_services_are_independent(client):
    from worker.main import claim as claim_infrastructure

    orders = [create(client, f"service-{i}", f"service-order-{i}") for i in range(2)]
    for order in orders:
        ready(order["instance_id"])
    for i in range(6):
        request = body(f"plain-{i}")["instance"]
        response = client.post(
            "/v1/instances", json=request, headers={"Idempotency-Key": f"plain-{i}"}
        )
        assert response.status_code == 202, response.text
    assert all(claim_infrastructure(f"infra-{i}") for i in range(6))
    assert claim_infrastructure("infra-extra") is None
    assert services.claim("service-a")
    assert services.claim("service-b")
    assert services.claim("service-extra") is None


def test_service_pool_capacity_can_expand_to_four(client, monkeypatch):
    monkeypatch.setenv("IACP_SERVICE_MAX_CONCURRENT", "4")
    orders = [
        create(client, f"service-capacity-{index}", f"capacity-{index}")
        for index in range(5)
    ]
    for order in orders:
        ready(order["instance_id"])
    with ThreadPoolExecutor(max_workers=5) as pool:
        claims = list(pool.map(services.claim, [f"service-{index}" for index in range(5)]))
    assert sum(claim is not None for claim in claims) == 4
    assert services.claim("service-extra") is None
    assert client.get("/v1/service-workers").json()["max_concurrent"] == 4


def test_order_validation_rolls_back_everything(client):
    invalid = body()
    invalid["instance"]["plan_id"] = "missing-plan"
    assert (
        client.post(
            "/v1/service-instances",
            json=invalid,
            headers={"Idempotency-Key": "bad-order"},
        ).status_code
        >= 400
    )
    with database() as db:
        for table in [
            "operations",
            "instances",
            "service_instances",
            "service_jobs",
            "idempotency_keys",
        ]:
            assert db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0


def test_release_manifest_cannot_change_on_retry(client):
    from worker.service_executor import pin_release, ServiceBlocked

    order = create(client)
    from api.service_catalog import load_service_release
    pinned = load_service_release("nextcloud")
    pin_release(order["service_id"], pinned)
    pin_release(order["service_id"], pinned)
    with pytest.raises(ServiceBlocked, match="SERVICE_RELEASE_CHANGED"):
        pin_release(order["service_id"], {"images": {"app": "second"}})


def test_services_allow_only_ubuntu_2404_and_ip_mode():
    from api.service_models import ServiceCreateRequest
    from pydantic import ValidationError
    body = {'application_id':'nextcloud','instance':{'name':'ip-service','plan_id':'kvm-1','image_id':'ubuntu-2404','network_profile':'lab_dhcp','access':{'ssh_public_keys':['ssh-ed25519 AAAAexample']}},'configuration':{'access_mode':'ip'}}
    assert ServiceCreateRequest.model_validate(body).configuration.domain is None
    body['configuration']['domain']='cloud.example.org'
    with pytest.raises(ValidationError):
        ServiceCreateRequest.model_validate(body)


@pytest.mark.parametrize("application_id", ["nextcloud", "n8n", "hermes", "openclaw"])
@pytest.mark.parametrize(
    "image_id",
    ["ubuntu-2604", "almalinux-9", "almalinux-10", "fedora-45", "debian-13"],
)
def test_service_requests_reject_every_non_ubuntu_2404_image(application_id, image_id):
    from api.service_models import ServiceCreateRequest
    from pydantic import ValidationError

    configuration = {"access_mode": "ip"} if application_id in {"nextcloud", "n8n"} else {"agent_mode": "local"}
    request = {
        "application_id": application_id,
        "instance": {
            "name": f"service-{application_id}-{image_id}",
            "plan_id": "kvm-1",
            "image_id": image_id,
            "access": {"root_password": "solo-prueba-local"},
        },
        "configuration": configuration,
    }
    with pytest.raises(ValidationError, match="imagen no está habilitada"):
        ServiceCreateRequest.model_validate(request)


def test_service_retry_rejects_historical_almalinux_instance(client):
    order = create(client, "historical-alma-service", "create-historical-alma")
    with database() as db:
        db.execute(
            "UPDATE instances SET image='almalinux-9' WHERE instance_id=?",
            (order["instance_id"],),
        )
        db.execute(
            "UPDATE service_jobs SET status='failed' WHERE job_id=?",
            (order["job_id"],),
        )
    response = client.post(
        f"/v1/service-jobs/{order['job_id']}/retry",
        json={},
        headers={"Idempotency-Key": "retry-historical-alma"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "SERVICE_IMAGE_RETIRED"
    with database() as db:
        status = db.execute(
            "SELECT status FROM service_jobs WHERE job_id=?", (order["job_id"],)
        ).fetchone()["status"]
    assert status == "failed"


def test_n8n_recipe_waits_for_database_and_migrations():
    from pathlib import Path
    release = json.loads((Path(__file__).parents[1] / 'services/releases.json').read_text())['n8n']
    compose, _ = render('n8n', {'domain':'n8n.example.invalid','access_mode':'domain'}, {
        'database_password':'ficticia-base-de-datos',
        'encryption_key':'ficticia-clave-de-cifrado',
    }, release)
    healthcheck = compose['services']['app']['healthcheck']
    assert '/healthz/readiness' in ' '.join(healthcheck['test'])
    assert healthcheck['start_period'] == '30s'
    assert compose['services']['app']['depends_on']['postgres']['condition'] == 'service_healthy'
    environment = compose['services']['app']['environment']
    assert environment['N8N_WEBHOOK_URL'] == 'https://n8n.example.invalid/'
    assert 'WEBHOOK_URL' not in environment


def test_n8n_recipe_leaves_authentication_to_n8n():
    from pathlib import Path
    release = json.loads((Path(__file__).parents[1] / 'services/releases.json').read_text())['n8n']
    compose, files = render('n8n', {'domain':'n8n.example.invalid','access_mode':'domain'}, {
        'database_password':'ficticia-base-de-datos',
        'encryption_key':'ficticia-clave-de-cifrado',
    }, release)
    caddyfile = files['Caddyfile']
    assert 'reverse_proxy app:5678' in caddyfile
    assert 'basic_auth' not in caddyfile
    assert 'onboarding_password' not in files
    assert compose['services']['app']['environment']['N8N_PROTOCOL'] == 'https'


def test_n8n_generates_owner_when_order_omits_credentials(client, monkeypatch):
    request = body('prueba-service-n8n-instrucciones')
    request['application_id'] = 'n8n'
    request['configuration'] = {'access_mode': 'ip'}
    order = client.post(
        '/v1/service-instances',
        json=request,
        headers={'Idempotency-Key': 'n8n-access-instructions'},
    ).json()
    with database() as db:
        stored = db.execute('SELECT secrets_encrypted, configuration_json FROM service_instances').fetchone()
        stored_secrets = decrypt(stored['secrets_encrypted'])
        assert 'admin_password' not in stored_secrets
        assert len(stored_secrets['owner_password']) >= 32
    ready(order['instance_id'])
    monkeypatch.setattr(services, 'execute', lambda *_: None)
    services.process('service-worker-test', services.claim('service-worker-test'))
    response = client.post(
        f"/v1/service-instances/{order['service_id']}/credentials"
    )
    assert response.status_code == 200
    access = response.json()
    assert access['owner_setup_required'] is False
    assert access['owner_email'] == json.loads(stored['configuration_json'])['owner_email']
    assert access['owner_password'] == stored_secrets['owner_password']
    assert 'onboarding_password' not in access


def test_nextcloud_gui_credentials_are_applied_encrypted_and_delivered_once(client, monkeypatch):
    request = body('prueba-nextcloud-gui-credentials')
    request['configuration'] = {'access_mode': 'ip', 'admin_user': 'cliente_admin'}
    request['secrets'] = {'admin_password': 'Nextcloud-Ficticio-2026'}
    response = client.post(
        '/v1/service-instances',
        json=request,
        headers={'Idempotency-Key': 'nextcloud-gui-credentials'},
    )
    assert response.status_code == 202, response.text
    order = response.json()
    assert 'Nextcloud-Ficticio-2026' not in response.text
    with database() as db:
        row = db.execute(
            'SELECT configuration_json, secrets_encrypted FROM service_instances WHERE service_id=?',
            (order['service_id'],),
        ).fetchone()
        stored = decrypt(row['secrets_encrypted'])
        assert json.loads(row['configuration_json'])['admin_user'] == 'cliente_admin'
        assert stored['admin_password'] == 'Nextcloud-Ficticio-2026'
        assert 'Nextcloud-Ficticio-2026' not in row['secrets_encrypted']
    ready(order['instance_id'])
    monkeypatch.setattr(services, 'execute', lambda *_: None)
    services.process('service-worker-test', services.claim('service-worker-test'))
    credentials = client.post(
        f"/v1/service-instances/{order['service_id']}/credentials"
    )
    assert credentials.status_code == 200
    assert credentials.json() == {
        'admin_user': 'cliente_admin',
        'admin_password': 'Nextcloud-Ficticio-2026',
    }
    assert credentials.headers['cache-control'] == 'no-store'
    assert client.post(
        f"/v1/service-instances/{order['service_id']}/credentials"
    ).status_code == 409


def test_n8n_owner_credentials_are_applied_encrypted_and_delivered_once(client, monkeypatch):
    request = body('prueba-n8n-owner-credentials')
    request['application_id'] = 'n8n'
    request['configuration'] = {
        'access_mode': 'ip',
        'owner_email': 'owner@example.invalid',
        'owner_first_name': 'Prueba',
        'owner_last_name': 'Local',
    }
    request['secrets'] = {'owner_password': 'N8n-Ficticio-2026'}
    response = client.post(
        '/v1/service-instances',
        json=request,
        headers={'Idempotency-Key': 'n8n-owner-credentials'},
    )
    assert response.status_code == 202, response.text
    order = response.json()
    assert 'N8n-Ficticio-2026' not in response.text
    with database() as db:
        row = db.execute(
            'SELECT secrets_encrypted FROM service_instances WHERE service_id=?',
            (order['service_id'],),
        ).fetchone()
        assert decrypt(row['secrets_encrypted'])['owner_password'] == 'N8n-Ficticio-2026'
        assert 'N8n-Ficticio-2026' not in row['secrets_encrypted']
    assert 'N8n-Ficticio-2026' not in client.get(
        f"/v1/service-instances/{order['service_id']}"
    ).text
    ready(order['instance_id'])
    monkeypatch.setattr(services, 'execute', lambda *_: None)
    services.process('service-worker-test', services.claim('service-worker-test'))
    credentials = client.post(
        f"/v1/service-instances/{order['service_id']}/credentials"
    )
    assert credentials.status_code == 200
    assert credentials.json() == {
        'owner_email': 'owner@example.invalid',
        'owner_first_name': 'Prueba',
        'owner_last_name': 'Local',
        'owner_password': 'N8n-Ficticio-2026',
        'owner_setup_required': False,
    }
    assert credentials.headers['cache-control'] == 'no-store'
    assert client.post(
        f"/v1/service-instances/{order['service_id']}/credentials"
    ).status_code == 409


@pytest.mark.no_database
def test_public_service_schema_documents_application_gui_credentials():
    from api.service_models import ServiceCreateRequest

    schema = ServiceCreateRequest.model_json_schema()["$defs"]
    configuration = schema["ApplicationConfiguration"]["properties"]
    secrets = schema["ApplicationSecrets"]["properties"]
    assert {"owner_email", "owner_first_name", "owner_last_name"}.issubset(
        configuration
    )
    assert "admin_user" in configuration
    assert "Nextcloud" in secrets["admin_password"]["description"]
    assert "n8n" in secrets["owner_password"]["description"]
    for field in ("admin_password", "owner_password"):
        password_schema = secrets[field]["anyOf"][0]
        assert password_schema["format"] == "password"
        assert password_schema["writeOnly"] is True
        assert password_schema["minLength"] == 12


@pytest.mark.no_database
def test_n8n_gui_credentials_require_all_fields_and_supported_password():
    from api.service_models import ServiceCreateRequest
    from pydantic import ValidationError

    request = body('invalid-n8n-gui-credentials')
    request['application_id'] = 'n8n'
    request['configuration'] = {'access_mode': 'ip', 'owner_email': 'owner@example.invalid'}
    request['secrets'] = {'owner_password': 'short'}
    with pytest.raises(ValidationError):
        ServiceCreateRequest.model_validate(request)


@pytest.mark.no_database
def test_n8n_owner_task_keeps_password_out_of_process_arguments():
    import base64
    from worker.service_executor import n8n_owner_setup_task

    password = 'N8n-Ficticio-2026'
    task = n8n_owner_setup_task(
        ['docker', 'compose', '--project-directory', '/tmp/service-test'],
        {
            'owner_email': 'owner@example.invalid',
            'owner_first_name': 'Prueba',
            'owner_last_name': 'Local',
        },
        {'owner_password': password},
    )
    command = task['ansible.builtin.command']
    assert password not in json.dumps(command['argv'])
    assert password not in command['stdin']
    assert json.loads(base64.b64decode(command['stdin']))['password'] == password
    assert 'owner/setup' in command['argv'][-1]
    assert '/login' in command['argv'][-1]
    assert task['no_log'] is True


def test_uncertain_cancellation_requires_explicit_stopped_confirmation(client):
    order=create(client)
    with database() as db:db.execute("UPDATE service_jobs SET status='uncertain' WHERE job_id=?",(order['job_id'],))
    path='/v1/service-jobs/'+order['job_id']+'/cancel'
    assert client.post(path,headers={'Idempotency-Key':'cancel-unconfirmed'}).status_code==409
    assert client.post(path,json={'previous_execution_stopped':True},headers={'Idempotency-Key':'cancel-reviewed'}).status_code==202
    with database() as db:assert db.execute('SELECT status FROM service_jobs WHERE job_id=?',(order['job_id'],)).fetchone()['status']=='cancelled'


def test_service_inventory_paginates_without_exposing_secrets(client, monkeypatch):
    first = create(client, name='prueba-service-nextcloud-uno', key='inventory-1')
    second = create(client, name='prueba-service-nextcloud-dos', key='inventory-2')
    application_deleted = create(client, name='prueba-service-nextcloud-retirado', key='inventory-3')
    infrastructure_deleted = create(client, name='prueba-service-nextcloud-kvm-retirada', key='inventory-4')
    with database() as db:
        db.execute("UPDATE service_instances SET status='deleted' WHERE service_id=?", (application_deleted['service_id'],))
        db.execute("UPDATE instances SET status='deleted' WHERE instance_id=?", (infrastructure_deleted['instance_id'],))
    response = client.get('/v1/service-instances?limit=1&offset=0')
    assert response.status_code == 200
    data = response.json()
    assert data['total'] == 2 and len(data['services']) == 1
    assert data['services'][0]['service_id'] == second['service_id']
    assert client.get('/v1/service-instances?limit=100').json()['total'] == 2
    assert 'secrets_encrypted' not in response.text
    assert 'admin_password' not in response.text
    assert client.get('/v1/service-instances?limit=0').status_code == 422
    detail = client.get('/v1/service-instances/'+first['service_id']).json()
    assert detail['name'] == 'prueba-service-nextcloud-uno'
    assert detail['credentials_available'] is False
    monkeypatch.setenv('IACP_REQUIRE_API_TOKEN', '1')
    monkeypatch.setenv('IACP_API_TOKEN', 'token-ficticio-inventario')
    assert client.get('/v1/service-instances').status_code == 401
