"""Limpieza segura de VMs que quedaron fuera del state de Terraform."""

import json

import pytest

from api.db import database, initialize
from worker import main as worker_main
from worker.proxmox import ProxmoxError


class FakeClient:
    def __init__(self, record):
        self.record = dict(record)
        self.deleted = []

    def inventory(self, uncertain=False, timeout=None, attempts=None):
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

    def get(self, path, uncertain=False, **_kwargs):
        if path == "/nodes":
            return [{"node": "mng01"}, {"node": "mng02"}]
        if path.startswith("/cluster/nextid?vmid="):
            return path.rsplit("=", 1)[1]
        if "/qemu/" in path and path.endswith("config?current=1"):
            raise ProxmoxError("VM_NOT_FOUND", status=404)
        return {"status": "stopped", "exitstatus": "OK"}


class PartialCloneClient(FakeClient):
    def __init__(self, record, *, tags="almalinux-9;golden-image;iacp", clone_proof=True):
        super().__init__(record)
        self.tags = tags
        self.clone_proof = clone_proof

    def vm_config(self, node, vmid):
        return {
            "name": self.record.get("name"),
            "template": self.record.get("template", 0),
            "tags": self.tags,
        }

    def completed_clone_task_during(self, node, target_vmid, source_vmid, started_at, finished_at):
        assert (node, target_vmid, source_vmid) == ("mng01", 40510, 29010)
        assert started_at and finished_at
        return self.clone_proof


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


def _preparar_prueba_clon_parcial(tmp_path, monkeypatch, *, state_tag=True):
    name = "qa-clon-parcial"
    owner = "op-parcial"
    marker = f"iacp-owned-{owner}"
    state_root = tmp_path / "plans" / owner
    workspace = state_root / "environments" / "mng01"
    workspace.mkdir(parents=True)
    tags = ["almalinux-9", "iacp-lab", "mng01"]
    if state_tag:
        tags.append(marker)
    state = {
        "resources": [{
            "mode": "managed",
            "type": "proxmox_virtual_environment_vm",
            "name": "this",
            "instances": [{"attributes": {
                "vm_id": 40510,
                "name": name,
                "node_name": "mng01",
                "pool_id": "iacp-lab",
                "template": False,
                "tags": tags,
            }}],
        }]
    }
    (workspace / "terraform.tfstate").write_text(json.dumps(state))
    payload = {
        "physical_identity_tag": marker,
        "spec": {"name": name, "image": "almalinux-9"},
    }
    source = {
        "operation_id": owner,
        "command": "provision",
        "status": "uncertain",
        "stack": "mng01",
        "started_at": "2026-03-01T00:00:01+00:00",
        "finished_at": "2026-03-01T00:00:10+00:00",
        "instance_id": "inst-parcial",
        "vmid": 40510,
        "target_node": "mng01",
        "workspace_path": str(state_root),
        "payload_json": json.dumps(payload),
    }
    identity = {
        "instance_id": "inst-parcial",
        "operation_id": owner,
        "name": name,
        "vmid": 40510,
        "node": "mng01",
        "status": "uncertain",
    }
    persisted_spec = {"name": name, "image": "almalinux-9"}
    events = []

    def check_lease(vmid, lease_owner):
        assert (vmid, lease_owner) == (40510, owner)

    monkeypatch.setattr(worker_main, "find_operation", lambda operation_id: source if operation_id == owner else None)
    monkeypatch.setattr(worker_main, "instance_identity", lambda instance_id: identity if instance_id == "inst-parcial" else {})
    monkeypatch.setattr(worker_main, "instance_spec", lambda instance_id: persisted_spec if instance_id == "inst-parcial" else {})
    monkeypatch.setattr(worker_main, "assert_lease_owner", check_lease)
    monkeypatch.setattr(worker_main, "emit", lambda *args, **kwargs: events.append(args))
    return name, owner, str(workspace), events


@pytest.mark.no_database
def test_destroy_acepta_clon_parcial_probado_por_state_y_tarea_qmclone(
    tmp_path, monkeypatch, entorno
):
    name, owner, workspace, _events = _preparar_prueba_clon_parcial(
        tmp_path, monkeypatch
    )
    client = PartialCloneClient(dict(RECORD, vmid=40510, name=name))
    assert worker_main.assert_destroy_physical_identity(
        client, 40510, "mng01", name, owner, workspace
    ) is None
    assert client.deleted == []


@pytest.mark.no_database
def test_destroy_rechaza_state_con_recursos_managed_adicionales(tmp_path, monkeypatch, entorno):
    from worker.proxmox import ProxmoxError

    name, owner, workspace, _events = _preparar_prueba_clon_parcial(
        tmp_path, monkeypatch
    )
    state_path = tmp_path / "plans" / owner / "environments" / "mng01" / "terraform.tfstate"
    state = json.loads(state_path.read_text())
    state["resources"].append(
        {
            "mode": "managed",
            "type": "terraform_data",
            "name": "unexpected",
            "instances": [{"attributes": {"id": "unrelated"}}],
        }
    )
    state_path.write_text(json.dumps(state))
    client = PartialCloneClient(dict(RECORD, vmid=40510, name=name))
    with pytest.raises(ProxmoxError, match="VM_PHYSICAL_OWNER_MISMATCH"):
        worker_main.assert_destroy_physical_identity(
            client, 40510, "mng01", name, owner, workspace
        )
    assert client.deleted == []


@pytest.mark.no_database
def test_destroy_rechaza_clon_si_la_imagen_persistida_no_coincide(
    tmp_path, monkeypatch, entorno
):
    from worker.proxmox import ProxmoxError

    name, owner, workspace, _events = _preparar_prueba_clon_parcial(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        worker_main,
        "instance_spec",
        lambda _instance_id: {"name": name, "image": "debian-13"},
    )
    client = PartialCloneClient(dict(RECORD, vmid=40510, name=name))
    with pytest.raises(ProxmoxError, match="VM_PHYSICAL_OWNER_MISMATCH"):
        worker_main.assert_destroy_physical_identity(
            client, 40510, "mng01", name, owner, workspace
        )
    assert client.deleted == []


@pytest.mark.no_database
@pytest.mark.parametrize(
    ("state_tag", "clone_proof", "tags"),
    [
        (False, True, "almalinux-9;golden-image;iacp"),
        (True, False, "almalinux-9;golden-image;iacp"),
        (True, True, "almalinux-9;iacp-owned-otra-operacion"),
    ],
)
def test_destroy_conserva_clon_si_falla_prueba_de_titularidad(
    tmp_path, monkeypatch, entorno, state_tag, clone_proof, tags
):
    from worker.proxmox import ProxmoxError

    name, owner, workspace, _events = _preparar_prueba_clon_parcial(
        tmp_path, monkeypatch, state_tag=state_tag
    )
    client = PartialCloneClient(
        dict(RECORD, vmid=40510, name=name),
        tags=tags,
        clone_proof=clone_proof,
    )
    with pytest.raises(ProxmoxError, match="VM_PHYSICAL_OWNER_MISMATCH"):
        worker_main.assert_destroy_physical_identity(
            client, 40510, "mng01", name, owner, workspace
        )
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
