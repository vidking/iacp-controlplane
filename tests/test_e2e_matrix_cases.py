"""Cobertura de la matriz física KVM: imágenes × planes × modos de acceso."""

import io
from collections import Counter
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from scripts import e2e_matrix

pytestmark = pytest.mark.no_database


def test_develop_cubre_48_casos_seis_imagenes_ambos_modos(monkeypatch):
    monkeypatch.setattr(e2e_matrix, "TARGET_STAGE", "develop")
    cases = e2e_matrix.build_matrix_cases()

    assert len(cases) == 48
    assert [case["index"] for case in cases] == list(range(1, 49))
    combinations = {(case["image"], case["plan"], case["mode"]) for case in cases}
    assert combinations == {
        (image, plan, mode)
        for image in e2e_matrix.IMAGES
        for plan in e2e_matrix.PLANS
        for mode in ("password", "key")
    }
    assert Counter(case["mode"] for case in cases) == {"password": 24, "key": 24}
    assert {"fedora-45", "debian-13"} <= {case["image"] for case in cases}


def test_inicio_explicito_y_automatico_no_dependen_del_modo(monkeypatch):
    monkeypatch.setattr(e2e_matrix, "TARGET_STAGE", "develop")
    cases = e2e_matrix.build_matrix_cases()

    by_mode = Counter((case["mode"], case["start_on_create"]) for case in cases)
    assert by_mode == {
        ("password", True): 12,
        ("password", False): 12,
        ("key", True): 12,
        ("key", False): 12,
    }


def test_production_cubre_tambien_las_seis_imagenes(monkeypatch):
    monkeypatch.setattr(e2e_matrix, "TARGET_STAGE", "production")
    cases = e2e_matrix.build_matrix_cases()

    assert len(cases) == 48
    assert {case["image"] for case in cases} == {
        "ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10", "fedora-45", "debian-13",
    }


def test_nombres_de_corrida_cubren_todos_los_casos():
    assert len(set(e2e_matrix.RUN_NAMES)) == e2e_matrix.MAX_CASES == 48
    assert e2e_matrix.case_name(48) == f"prueba-kvm-48-{e2e_matrix.RUN_ID}"
    assert all(len(name) <= 63 for name in e2e_matrix.RUN_NAMES)


def test_ssh_command_valida_la_salida_esperada(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("x")
    key.chmod(0o600)

    class Completed:
        returncode = 0
        stdout = "otro-valor\n"
        stderr = ""

    monkeypatch.setattr(e2e_matrix.subprocess, "run", lambda *args, **kwargs: Completed())
    with pytest.raises(e2e_matrix.E2EError):
        e2e_matrix.ssh_command("192.0.2.10", "key", private_key=key, command="cat f", expected="token")
    assert (
        e2e_matrix.ssh_command("192.0.2.10", "key", private_key=key, command="cat f", expected=None)
        == "otro-valor"
    )


def _valid_config(monkeypatch, tmp_path, profile):
    monkeypatch.setattr(e2e_matrix, "TARGET_STAGE", "production")
    monkeypatch.setattr(e2e_matrix, "BASE_URL", "https://iac.cyberfuel.com")
    monkeypatch.setattr(e2e_matrix, "API_TOKEN", "token-de-prueba")
    monkeypatch.setattr(e2e_matrix, "PROXMOX_ENDPOINT", "https://mng01.example:8006")
    monkeypatch.setattr(e2e_matrix, "PROXMOX_TOKEN", "proxmox-de-prueba")
    monkeypatch.setattr(e2e_matrix, "RUN_ID", "20261120000000")
    monkeypatch.setattr(e2e_matrix, "MAX_WORKERS", 6)
    monkeypatch.setattr(e2e_matrix, "VMID_MIN", 30000)
    monkeypatch.setattr(e2e_matrix, "VMID_MAX", 39999)
    monkeypatch.setattr(e2e_matrix, "EVIDENCE_FILE", tmp_path / "report.json")
    monkeypatch.setattr(e2e_matrix, "NETWORK_PROFILE", profile)


@pytest.mark.parametrize("profile", ["lab_dhcp", "prod_static"])
def test_perfiles_de_red_admitidos(monkeypatch, tmp_path, profile):
    _valid_config(monkeypatch, tmp_path, profile)
    e2e_matrix.validate_execution_config()


def test_perfil_de_red_desconocido_se_rechaza(monkeypatch, tmp_path):
    _valid_config(monkeypatch, tmp_path, "prod_dhcp")
    with pytest.raises(e2e_matrix.E2EError, match="lab_dhcp o prod_static"):
        e2e_matrix.validate_execution_config()


def test_catalogo_exige_modo_estatico_para_prod_static(monkeypatch):
    monkeypatch.setattr(e2e_matrix, "NETWORK_PROFILE", "prod_static")
    monkeypatch.setattr(e2e_matrix, "TARGET_STAGE", "production")
    responses = {
        "/v1/environments": {"default_environment": "lab", "environments": [{"environment": "lab"}]},
        "/v1/plans": {"plans": [{"plan_id": plan} for plan in e2e_matrix.PLANS]},
        "/v1/images": {"images": [{"image_id": image} for image in e2e_matrix.IMAGES]},
        "/v1/network-profiles": {"network_profiles": [
            {"profile_id": "lab_dhcp", "enabled": False, "network_mode": "dhcp"},
            {"profile_id": "prod_static", "enabled": True, "network_mode": "static"},
        ]},
        "/v1/observability": {"max_concurrent": 6},
    }
    monkeypatch.setattr(e2e_matrix, "api", lambda method, path, **kwargs: responses[path])
    e2e_matrix.validate_catalog()

    responses["/v1/network-profiles"]["network_profiles"][1]["network_mode"] = "dhcp"
    with pytest.raises(e2e_matrix.E2EError, match="no disponible"):
        e2e_matrix.validate_catalog()


def test_filtro_almalinux_genera_16_casos(monkeypatch):
    monkeypatch.setenv("IACP_E2E_IMAGES", "almalinux-9,almalinux-10")
    cases = e2e_matrix.build_matrix_cases()
    assert len(cases) == 16
    assert Counter(case["image"] for case in cases) == {"almalinux-9": 8, "almalinux-10": 8}
    assert Counter(case["mode"] for case in cases) == {"password": 8, "key": 8}
    assert {case["plan"] for case in cases} == set(e2e_matrix.PLANS)
    assert {case["start_on_create"] for case in cases} == {True, False}


def test_filtro_rechaza_imagenes_no_publicadas(monkeypatch):
    monkeypatch.setenv("IACP_E2E_IMAGES", "almalinux-9,centos-7")
    with pytest.raises(e2e_matrix.E2EError, match="no publicadas"):
        e2e_matrix.build_matrix_cases()


def test_http_json_reintenta_429_en_lecturas_idempotentes(monkeypatch):
    attempts = []
    headers = Message()
    headers["Retry-After"] = "0"

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ready":true}'

    def open_request(_request, timeout):
        assert timeout == 30
        attempts.append(1)
        if len(attempts) == 1:
            raise HTTPError("https://dev.example/v1/workers", 429, "Too Many Requests", headers, io.BytesIO(b"{}"))
        return Response()

    monkeypatch.setattr(e2e_matrix, "build_opener", lambda: SimpleNamespace(open=open_request))
    monkeypatch.setattr(e2e_matrix, "_API_LAST_REQUEST", 0.0)
    monkeypatch.setattr(e2e_matrix.time, "sleep", lambda _delay: None)
    retries = []
    monkeypatch.setattr(e2e_matrix, "_API_RETRIES", retries)

    status, payload = e2e_matrix.http_json("GET", "https://dev.example/v1/workers")

    assert status == 200
    assert payload == {"ready": True}
    assert len(attempts) == 2
    assert retries[-1]["endpoint"] == "/v1/workers"
    assert retries[-1]["retry_after"] == "0"


def test_http_json_no_reintenta_mutacion_sin_idempotency_key(monkeypatch):
    attempts = []

    def open_request(_request, timeout):
        attempts.append(1)
        raise HTTPError(
            "https://dev.example/v1/instances",
            429,
            "Too Many Requests",
            Message(),
            io.BytesIO(b'{"detail":"busy"}'),
        )

    monkeypatch.setattr(e2e_matrix, "build_opener", lambda: SimpleNamespace(open=open_request))
    monkeypatch.setattr(e2e_matrix, "_API_LAST_REQUEST", 0.0)
    monkeypatch.setattr(e2e_matrix.time, "sleep", lambda _delay: None)

    with pytest.raises(e2e_matrix.E2EError) as error:
        e2e_matrix.http_json("POST", "https://dev.example/v1/instances", body={"name": "vm"})

    assert error.value.status == 429
    assert len(attempts) == 1


def test_http_json_reintenta_mutacion_con_idempotency_key(monkeypatch):
    attempts = []
    headers = Message()
    headers["Retry-After"] = "0"

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"operation_id":"op-1"}'

    def open_request(request, timeout):
        assert request.get_header("Idempotency-key") == "create-key"
        attempts.append(1)
        if len(attempts) == 1:
            raise HTTPError("https://dev.example/v1/instances", 429, "Too Many Requests", headers, io.BytesIO(b"{}"))
        return Response()

    monkeypatch.setattr(e2e_matrix, "build_opener", lambda: SimpleNamespace(open=open_request))
    monkeypatch.setattr(e2e_matrix, "_API_LAST_REQUEST", 0.0)
    monkeypatch.setattr(e2e_matrix.time, "sleep", lambda _delay: None)

    status, payload = e2e_matrix.http_json(
        "POST",
        "https://dev.example/v1/instances",
        body={"name": "vm"},
        headers={"Idempotency-Key": "create-key"},
    )

    assert status == 202
    assert payload == {"operation_id": "op-1"}
    assert len(attempts) == 2


def test_cleanup_confirma_destroy_por_api_si_proxmox_bloquea_lectura(monkeypatch):
    initial = {
        "status": "ready",
        "vmid": 40042,
        "node": "mng01",
        "latest_operation": {"operation_id": "provision-op", "status": "completed", "command": "provision"},
    }
    deleted = {"status": "deleted", "vmid": 40042, "node": "mng01"}
    instances = iter([initial, deleted, deleted])
    calls = []

    monkeypatch.setattr(e2e_matrix, "instance", lambda _instance_id: next(instances))
    monkeypatch.setattr(e2e_matrix, "power", lambda _instance_id: {"power_state": "stopped"})
    monkeypatch.setattr(e2e_matrix, "wait_operation", lambda _operation: {"status": "completed"})
    def forbidden_vmid_read(*_args):
        raise e2e_matrix.E2EError("forbidden", status=403)

    monkeypatch.setattr(e2e_matrix, "vm_exists", forbidden_vmid_read)
    monkeypatch.setattr(e2e_matrix, "release_live_ip", lambda _instance_id: None)

    def api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "DELETE":
            return {"operation_id": "destroy-op"}
        return {"status": "completed", "command": "destroy", "exit_code": 0}

    monkeypatch.setattr(e2e_matrix, "api", api)

    result = e2e_matrix.cleanup("instance-id", "case-1")

    assert result == "destroyed:terraform_destroy_completed_proxmox_read_forbidden"
    assert calls[0][0] == "DELETE"
    assert calls[0][2]["idempotency_key"] == "case-1-cleanup-destroy"
