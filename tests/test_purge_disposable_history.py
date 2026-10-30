from types import SimpleNamespace

import pytest

from iacp.maintenance.purge import (
    DELETE_ORDER,
    PurgeError,
    PurgeTarget,
    parse_vmid_range,
    run_purge,
    total_purge_confirmation,
    validate_fence_evidence,
    validate_total_purge_confirmation,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30000-30104", (30000, 30104)),
        ("30000–30104", (30000, 30104)),
        (" 30100 : 30102 ", (30100, 30102)),
    ],
)
def test_parsea_rangos_vmid_validos(value, expected):
    assert parse_vmid_range(value) == expected


@pytest.mark.parametrize("value", ["30104-30000", "abc-30104", "99-100", "100-1000000000"])
def test_rechaza_rangos_vmid_invalidos(value):
    with pytest.raises(PurgeError):
        parse_vmid_range(value)


def test_purga_total_requiere_confirmacion_exacta():
    assert total_purge_confirmation(30000, 30104) == "PURGAR TOTAL 30000-30104"
    validate_total_purge_confirmation(30000, 30104, "PURGAR TOTAL 30000-30104")
    with pytest.raises(PurgeError, match="CONFIRMATION"):
        validate_total_purge_confirmation(30000, 30104, "purga total 30000-30104")


def test_el_orden_de_purga_respeta_dependencias_foreign_key():
    assert DELETE_ORDER.index("instances") == len(DELETE_ORDER) - 1
    assert DELETE_ORDER.index("operation_events") < DELETE_ORDER.index("operations")
    assert DELETE_ORDER.index("terraform_applies") < DELETE_ORDER.index("terraform_executions")
    assert DELETE_ORDER.index("jobs") < DELETE_ORDER.index("operations")


def test_fence_readonly_debe_ligar_operacion_exacta():
    target = PurgeTarget(
        instance_id="11111111-1111-4111-8111-111111111111",
        tenant_id="00000000-0000-0000-0000-000000000001",
        vmid=30096,
        node="mng01",
        name="historica",
        state_key="iacp/lab/tenants/t/instances/i/terraform.tfstate",
        allocation={"backend": {"bucket": "example-iacp-lab", "region": "us-east-1", "endpoint": "https://s3.wasabisys.com"}},
    )
    operation = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222",
        status="uncertain",
    )
    job = SimpleNamespace(status="failed")
    evidence = {
        "evidence_version": "iacp.readonly-operation-fence.v1",
        "operation_id": operation.id,
        "instance_id": target.instance_id,
        "vmid": target.vmid,
        "operation_kind": "instance.access.bootstrap",
        "operation_status": "uncertain",
        "job_status": "failed",
        "redelivery_mode": "observe-only-fenced",
        "proxmox_present": False,
        "state_resource_present": False,
        "state_lock_absent": True,
        "state_resource_id": None,
        "state_snapshot": {"version_id": "v1", "lineage": "l", "serial": 1},
    }
    validate_fence_evidence(evidence, target, operation, job)

    evidence["instance_id"] = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(PurgeError, match="BINDING"):
        validate_fence_evidence(evidence, target, operation, job)


def test_purga_vacia_es_idempotente_y_no_muta(engine, sessions):
    result = run_purge(
        vmid_range="30000-30104",
        total_purge=True,
        confirmation="PURGAR TOTAL 30000-30104",
        execute=True,
        session_factory=sessions,
        cluster_vmids_fn=lambda: set(),
        runtime_idle_fn=lambda: {"idle": True},
    )
    assert result["passed"] is True
    assert result["changed"] is False
    assert result["target_count"] == 0
