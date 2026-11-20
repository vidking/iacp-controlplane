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
        for image in e2e_matrix.IMAGES + e2e_matrix.DEVELOP_EXTRA_IMAGES
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


def test_production_conserva_las_imagenes_publicadas(monkeypatch):
    monkeypatch.setattr(e2e_matrix, "TARGET_STAGE", "production")
    cases = e2e_matrix.build_matrix_cases()

    assert len(cases) == 32
    assert {case["image"] for case in cases} == set(e2e_matrix.IMAGES)


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
