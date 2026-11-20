"""Cobertura de la matriz física KVM: imágenes × planes × modos de acceso."""

from collections import Counter

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
