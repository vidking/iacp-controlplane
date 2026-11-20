"""Limpieza segura de VMs que quedaron fuera del state de Terraform."""

import json

import pytest

from api.db import database, initialize
from worker import main as worker_main


class FakeClient:
    def __init__(self, record):
        self.record = dict(record)
        self.deleted = []

    def inventory(self, uncertain=False):
        return [self.record] if self.record else []

    def permissions(self, node, vmid, mutate=False):
        return None

    def vm_config(self, node, vmid):
        return {"name": self.record.get("name"), "template": self.record.get("template", 0)}

    def _request(self, method, path, parameters, uncertain=False):
        assert method == "DELETE" and "purge=1" in path
        self.deleted.append(path)
        self.record = {}
        return "UPID:mng01:1:1:1:qmdestroy:40510:t@pam!x:"

    def get(self, path, uncertain=False):
        return {"status": "stopped", "exitstatus": "OK"}


def _state(tmp_path, vm_ids):
    resources = [
        {"type": "proxmox_virtual_environment_vm", "instances": [{"attributes": {"vm_id": vm_id}} for vm_id in vm_ids]}
    ] if vm_ids is not None else []
    (tmp_path / "terraform.tfstate").write_text(json.dumps({"resources": resources}))
    return str(tmp_path)


def _seed(instance_name="qa-huerfana", lease_instance="inst-1"):
    initialize()
    with database() as db:
        db.execute(
            """INSERT INTO operations(operation_id, command, action, status, payload_json, stack, created_at, instance_id)
               VALUES ('op-prov', 'provision', 'provision', 'failed', '{}', 'mng01', 'x', ?)""",
            (lease_instance,),
        )
        db.execute(
            """INSERT INTO instances(instance_id, operation_id, name, image, plan_id, vmid, environment,
                                     status, spec_json, commercial_context_json, created_at, updated_at, node)
               VALUES ('inst-1', 'op-prov', ?, 'almalinux-9', 'kvm-3', 40510, 'lab',
                       'uncertain', '{}', '{}', 'x', 'x', 'mng01')""",
            (instance_name,),
        )
        db.execute(
            "INSERT INTO vmid_leases(vmid, operation_id, status, created_at) VALUES (40510, 'op-prov', 'held', 'x')"
        )


RECORD = {"vmid": 40510, "name": "qa-huerfana", "node": "mng01", "pool": "iacp-lab", "status": "stopped", "template": 0}


@pytest.fixture
def entorno(monkeypatch):
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "49999")
    monkeypatch.setenv("IACP_PROXMOX_POOL_ID", "iacp-lab")
    monkeypatch.setenv("IACP_RECONCILE_TIMEOUT", "5")
    monkeypatch.setattr(worker_main.time, "sleep", lambda _: None)


@pytest.mark.no_database
def test_state_demuestra_huerfana(tmp_path):
    assert worker_main.terraform_state_tracks_vmid(_state(tmp_path, None), 40510) is False
    assert worker_main.terraform_state_tracks_vmid(_state(tmp_path, [40511]), 40510) is False
    assert worker_main.terraform_state_tracks_vmid(_state(tmp_path, [40510]), 40510) is True
    assert worker_main.terraform_state_tracks_vmid(_state(tmp_path, [None]), 40510) is True
    assert worker_main.terraform_state_tracks_vmid(str(tmp_path / "no-existe"), 40510) is None


def test_elimina_huerfana_con_identidad_verificada(tmp_path, entorno):
    _seed()
    client = FakeClient(RECORD)
    assert worker_main.remove_orphan_vm(client, "op-prov", "inst-1", 40510, _state(tmp_path, None)) is True
    assert len(client.deleted) == 1
    with database() as db:
        kinds = [row["kind"] for row in db.execute("SELECT kind FROM events WHERE operation_id='op-prov'")]
    assert "destroy.orphan.completed" in kinds


@pytest.mark.parametrize(
    ("cambio", "motivo"),
    [
        ({"name": "otra-vm"}, "nombre_distinto"),
        ({"pool": "otro-pool"}, "pool_distinto"),
        ({"status": "running"}, "vm_no_detenida"),
        ({"template": 1}, "es_template"),
        ({"node": "mng02"}, "nodo_distinto"),
    ],
)
def test_conserva_vm_si_alguna_guarda_falla(tmp_path, entorno, cambio, motivo):
    _seed()
    client = FakeClient(dict(RECORD, **cambio))
    assert worker_main.remove_orphan_vm(client, "op-prov", "inst-1", 40510, _state(tmp_path, None)) is False
    assert client.deleted == []
    with database() as db:
        detail = db.execute(
            "SELECT detail FROM events WHERE operation_id='op-prov' AND kind='destroy.orphan.retained'"
        ).fetchone()["detail"]
    assert motivo in detail


def test_conserva_vm_registrada_en_state_o_fuera_de_rango(tmp_path, entorno, monkeypatch):
    _seed()
    client = FakeClient(RECORD)
    assert worker_main.remove_orphan_vm(client, "op-prov", "inst-1", 40510, _state(tmp_path, [40510])) is False
    monkeypatch.setenv("IACP_VMID_MAX", "40100")
    assert worker_main.remove_orphan_vm(client, "op-prov", "inst-1", 40510, _state(tmp_path, None)) is False
    assert client.deleted == []


def test_conserva_vm_con_lease_de_otra_instancia(tmp_path, entorno):
    _seed(lease_instance="inst-ajena")
    client = FakeClient(RECORD)
    assert worker_main.remove_orphan_vm(client, "op-prov", "inst-1", 40510, _state(tmp_path, None)) is False
    assert client.deleted == []


def test_cierra_reconcile_superado_cuando_el_origen_termino():
    initialize()
    with database() as db:
        for operation_id, command, status, payload in (
            ("origen", "destroy", "succeeded", "{}"),
            ("intento-viejo", "reconcile", "uncertain", '{"source_operation_id": "origen"}'),
            ("origen-abierto", "destroy", "uncertain", "{}"),
            ("intento-vigente", "reconcile", "uncertain", '{"source_operation_id": "origen-abierto"}'),
        ):
            db.execute(
                "INSERT INTO operations(operation_id, command, action, status, payload_json, stack, created_at) VALUES (?, ?, ?, ?, ?, 'mng01', 'x')",
                (operation_id, command, command, status, payload),
            )
    assert worker_main.close_resolved_reconcile_attempts() == 1
    with database() as db:
        rows = {r["operation_id"]: (r["status"], r["phase"]) for r in db.execute("SELECT operation_id, status, phase FROM operations")}
    assert rows["intento-viejo"] == ("failed", "superseded")
    assert rows["intento-vigente"][0] == "uncertain"
    assert rows["origen-abierto"][0] == "uncertain"
    assert worker_main.close_resolved_reconcile_attempts() == 0
