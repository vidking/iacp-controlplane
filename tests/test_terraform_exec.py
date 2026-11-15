"""Ejecución de Terraform: timeouts, salida acotada, entorno y redacción de secretos."""

import os
import sys
import time

import pytest

import worker.main as worker_main


@pytest.fixture(autouse=True)
def no_heartbeat(monkeypatch):
    monkeypatch.setattr(worker_main, "heartbeat", lambda *args, **kwargs: None)


def test_timeout_terminates_the_command_promptly(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_TIMEOUT", "2")
    monkeypatch.setenv("IACP_TERRAFORM_KILL_GRACE_SECONDS", "1")
    marker = tmp_path / "still-running"
    script = (
        "import time, pathlib\n"
        "time.sleep(8)\n"
        f"pathlib.Path({str(marker)!r}).write_text('vivo')\n"
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        worker_main.run_commands("w", "op-timeout", str(tmp_path), [[sys.executable, "-c", script]])
    assert time.monotonic() - started < 8
    time.sleep(7)
    assert not marker.exists(), "el proceso hijo siguió vivo después del timeout"


def test_terraform_environment_excludes_unrelated_credentials():
    environment = worker_main.terraform_environment(
        {
            "PATH": "/usr/bin",
            "PGPASSWORD": "postgres-secret",
            "POSTGRES_PASSWORD": "postgres-secret",
            "IACP_DATABASE_URL": "postgresql://iacp:secret@postgres/iacp",
            "IACP_API_TOKEN": "api-token-secret",
            "IACP_NETBOX_TOKEN": "netbox-secret",
            "IACP_WASABI_SECRET_ACCESS_KEY": "wasabi-secret",
            "IACP_PROXMOX_MNG_API_TOKEN": "user@pve!iacp=uuid",
            "PROXMOX_VE_ENDPOINT": "https://mng01.example:8006/",
            "PROXMOX_VE_API_TOKEN": "user@pve!iacp=uuid",
            "TF_PLUGIN_CACHE_DIR": "/opt/cache",
        }
    )
    assert environment == {
        "PATH": "/usr/bin",
        "PROXMOX_VE_ENDPOINT": "https://mng01.example:8006/",
        "PROXMOX_VE_API_TOKEN": "user@pve!iacp=uuid",
        "TF_PLUGIN_CACHE_DIR": "/opt/cache",
    }


def test_child_process_does_not_receive_database_password(tmp_path, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "postgres-secret-value")
    script = "import os; print(os.environ.get('PGPASSWORD', 'ausente'))"
    code, output, _ = worker_main.run_commands("w", "op-env", str(tmp_path), [[sys.executable, "-c", script]])
    assert code == 0
    assert output.strip() == "ausente"


def test_output_redacts_root_password_and_proxmox_token(tmp_path, monkeypatch):
    secret = tmp_path / "root_password"
    secret.write_text("RootPassword!2026\n", encoding="utf-8")
    monkeypatch.setenv("PROXMOX_VE_API_TOKEN", "iacp@pve!worker=0f8e3c2a-aaaa-bbbb-cccc-123456789abc")
    script = (
        "import os, sys\n"
        "print('pwd=' + os.environ['TF_VAR_root_password'])\n"
        "sys.stderr.write('token=0f8e3c2a-aaaa-bbbb-cccc-123456789abc')\n"
    )
    code, output, error = worker_main.run_commands(
        "w", "op-redact", str(tmp_path), [[sys.executable, "-c", script]], secret_path=str(secret)
    )
    assert code == 0
    assert "RootPassword!2026" not in output
    assert "0f8e3c2a-aaaa-bbbb-cccc-123456789abc" not in error
    assert "<redacted>" in output and "<redacted>" in error


def test_large_output_is_summarised_without_loading_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_OPERATION_OUTPUT_MAX_CHARS", "4096")
    script = "import sys\nfor i in range(200000): sys.stdout.write('linea %d\\n' % i)\nsys.stdout.write('FIN')"
    code, output, _ = worker_main.run_commands("w", "op-big", str(tmp_path), [[sys.executable, "-c", script]])
    assert code == 0
    assert len(output) <= 4096
    assert output.startswith("linea 0")
    assert output.endswith("FIN")
    # Los archivos temporales sin nombre no quedan en el workspace.
    assert os.listdir(tmp_path) == []


def test_nonzero_exit_code_is_returned(tmp_path):
    code, _, error = worker_main.run_commands(
        "w", "op-exit", str(tmp_path), [[sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(7)"]]
    )
    assert code == 7
    assert "boom" in error


def test_heartbeat_failure_does_not_abandon_running_command(tmp_path, monkeypatch):
    import psycopg

    def failing_heartbeat(*_args, **_kwargs):
        raise psycopg.OperationalError("PostgreSQL no disponible")

    monkeypatch.setattr(worker_main, "heartbeat", failing_heartbeat)
    monkeypatch.setenv("IACP_COMMAND_HEARTBEAT_SECONDS", "1")
    code, output, _ = worker_main.run_commands(
        "w", "op-hb", str(tmp_path), [[sys.executable, "-c", "import time; time.sleep(2.5); print('terminado')"]]
    )
    assert code == 0
    assert "terminado" in output
