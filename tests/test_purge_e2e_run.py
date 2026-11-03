from __future__ import annotations

import json

import pytest

from scripts.purge_e2e_run import (
    IMAGES,
    PLANS,
    PurgeRefused,
    _instance_names,
    _load_evidence,
    _sql_placeholders,
    _verify_state,
)
from scripts import e2e_matrix


RUN_ID = "20261103123456"


def _report():
    results = []
    for index, (image, plan) in enumerate(
        ((image, plan) for image in IMAGES for plan in PLANS),
        start=1,
    ):
        name = f"e2e-iac-prod-mng-{RUN_ID}-{index:02d}-{image.replace('-', '')}-{plan}"
        results.append(
            {
                "name": name,
                "instance_id": f"instance-{index}",
                "create_operation_id": f"create-{index}",
                "operation_ids": [f"create-{index}", f"destroy-{index}"],
                "status": "passed",
                "vmid": 30000 + index,
            }
        )
    return {
        "run_id": RUN_ID,
        "base_url": "https://iac.cyberfuel.com",
        "runtime_stage": "production",
        "environment": "lab",
        "cluster": "MNG",
        "vmid_range": [30000, 39999],
        "max_simultaneous_provisions": 6,
        "images": list(IMAGES),
        "plans": list(PLANS),
        "outcome": "PASS",
        "metrics": {
            "contention_detected": False,
            "sampling_errors": [],
            "foreign_activity": [],
            "peak_test_busy_workers": 6,
        },
        "results": results,
    }


def _resource(name: str, vmid: int) -> dict:
    return {
        "mode": "managed",
        "type": "proxmox_virtual_environment_vm",
        "name": "this",
        "provider": "provider[\"registry.terraform.io/bpg/proxmox\"]",
        "instances": [{"schema_version": 0, "attributes": {"name": name, "vm_id": vmid}}],
    }


def test_purge_sql_placeholders_match_all_lifecycle_operation_ids():
    assert _sql_placeholders(72).count("?") == 72


def test_purge_run_names_cover_the_16_unique_image_plan_pairs():
    names = _instance_names(RUN_ID)
    assert len(names) == 16
    assert len({(image, plan) for image in IMAGES for plan in PLANS}) == 16
    assert all(name.startswith(f"e2e-iac-prod-mng-{RUN_ID}-") for name in names)


def test_physical_matrix_refuses_the_development_api(monkeypatch):
    monkeypatch.setattr(e2e_matrix, "BASE_URL", "https://dev-cyberfuel.com")

    with pytest.raises(e2e_matrix.E2EError, match="iac.cyberfuel.com"):
        e2e_matrix.validate_execution_config()


def test_physical_matrix_requires_six_simultaneous_provisions(monkeypatch):
    monkeypatch.setattr(e2e_matrix, "BASE_URL", "https://iac.cyberfuel.com")
    monkeypatch.setattr(e2e_matrix, "API_TOKEN", "api-test-token")
    monkeypatch.setattr(e2e_matrix, "PROXMOX_ENDPOINT", "https://mng01.cyberfuel.com:8006")
    monkeypatch.setattr(e2e_matrix, "PROXMOX_TOKEN", "mng-test-token")
    monkeypatch.setattr(e2e_matrix, "RUN_ID", RUN_ID)
    monkeypatch.setattr(e2e_matrix, "MAX_WORKERS", 5)
    monkeypatch.setattr(e2e_matrix, "VMID_MIN", 30000)
    monkeypatch.setattr(e2e_matrix, "VMID_MAX", 39999)
    monkeypatch.setattr(e2e_matrix, "NETWORK_PROFILE", "lab_dhcp")

    with pytest.raises(e2e_matrix.E2EError, match="seis aprovisionamientos"):
        e2e_matrix.validate_execution_config()


def test_external_pass_evidence_matches_this_run(tmp_path):
    workdata = tmp_path / "workdata"
    workdata.mkdir()
    evidence = tmp_path / "evidence" / f"{RUN_ID}.json"
    evidence.parent.mkdir()
    evidence.write_text(json.dumps(_report()), encoding="utf-8")

    report, digest = _load_evidence(RUN_ID, evidence, workdata)

    assert report["outcome"] == "PASS"
    assert len(digest) == 64


def test_purge_evidence_requires_six_simultaneous_provisions(tmp_path):
    workdata = tmp_path / "workdata"
    workdata.mkdir()
    evidence = tmp_path / "evidence.json"
    report = _report()
    report["max_simultaneous_provisions"] = 2
    evidence.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(PurgeRefused, match="no certifica PASS"):
        _load_evidence(RUN_ID, evidence, workdata)


def test_evidence_inside_workdata_cannot_authorize_purge(tmp_path):
    workdata = tmp_path / "workdata"
    workdata.mkdir()
    evidence = workdata / "evidence.json"
    evidence.write_text(json.dumps(_report()), encoding="utf-8")

    with pytest.raises(PurgeRefused, match="fuera del volumen"):
        _load_evidence(RUN_ID, evidence, workdata)


def test_evidence_with_failed_case_cannot_authorize_purge(tmp_path):
    workdata = tmp_path / "workdata"
    workdata.mkdir()
    evidence = tmp_path / "evidence.json"
    report = _report()
    report["results"][0]["status"] = "failed"
    evidence.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(PurgeRefused, match="caso incompleto"):
        _load_evidence(RUN_ID, evidence, workdata)


def test_empty_active_state_and_same_instance_backup_are_safe_to_purge(tmp_path):
    name = "e2e-iac-prod-mng-20261103123456-01-ubuntu2404-kvm-1"
    workspace = tmp_path / "plans" / "operation-id" / "terraform"
    state_dir = workspace / "environments" / "mng01"
    state_dir.mkdir(parents=True)
    active = {"version": 4, "serial": 2, "lineage": "test", "outputs": {}, "resources": []}
    backup = {"version": 4, "serial": 1, "lineage": "test", "outputs": {}, "resources": [_resource(name, 30001)]}
    (state_dir / "terraform.tfstate").write_text(json.dumps(active), encoding="utf-8")
    (state_dir / "terraform.tfstate.backup").write_text(json.dumps(backup), encoding="utf-8")

    result = _verify_state(workspace, name, 30001)

    assert result == {"state_files": 2, "backup_resource_references": 1}


def test_active_state_with_resource_refuses_purge(tmp_path):
    name = "e2e-iac-prod-mng-20261103123456-01-ubuntu2404-kvm-1"
    workspace = tmp_path / "plans" / "operation-id" / "terraform"
    state_dir = workspace / "environments" / "mng01"
    state_dir.mkdir(parents=True)
    active = {"version": 4, "serial": 2, "lineage": "test", "resources": [_resource(name, 30001)]}
    (state_dir / "terraform.tfstate").write_text(json.dumps(active), encoding="utf-8")

    with pytest.raises(PurgeRefused, match="State activo"):
        _verify_state(workspace, name, 30001)


def test_backup_referencing_another_vm_refuses_purge(tmp_path):
    name = "e2e-iac-prod-mng-20261103123456-01-ubuntu2404-kvm-1"
    workspace = tmp_path / "plans" / "operation-id" / "terraform"
    state_dir = workspace / "environments" / "mng01"
    state_dir.mkdir(parents=True)
    active = {"version": 4, "serial": 2, "lineage": "test", "resources": []}
    backup = {"version": 4, "serial": 1, "lineage": "test", "resources": [_resource("other-vm", 30002)]}
    (state_dir / "terraform.tfstate").write_text(json.dumps(active), encoding="utf-8")
    (state_dir / "terraform.tfstate.backup").write_text(json.dumps(backup), encoding="utf-8")

    with pytest.raises(PurgeRefused, match="fuera de esta instancia"):
        _verify_state(workspace, name, 30001)


def test_terraform_lock_refuses_purge(tmp_path):
    name = "e2e-iac-prod-mng-20261103123456-01-ubuntu2404-kvm-1"
    workspace = tmp_path / "plans" / "operation-id" / "terraform"
    state_dir = workspace / "environments" / "mng01"
    state_dir.mkdir(parents=True)
    (state_dir / "terraform.tfstate").write_text(
        json.dumps({"version": 4, "serial": 2, "lineage": "test", "resources": []}),
        encoding="utf-8",
    )
    (state_dir / ".terraform.tfstate.lock.info").write_text("{}", encoding="utf-8")

    with pytest.raises(PurgeRefused, match="lock de Terraform"):
        _verify_state(workspace, name, 30001)
