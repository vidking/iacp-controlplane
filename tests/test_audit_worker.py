import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.db import database, initialize
from worker import main
from worker.allocation import allocate, release
from worker.heartbeat import heartbeat
from worker.netbox import NetBoxClient, NetBoxError
from worker.proxmox import ProxmoxClient, ProxmoxError


class Inventory:
    def inventory(self, *, uncertain=False):
        return []


def test_liberacion_exige_dueño_actual_del_vmid(monkeypatch):
    initialize()
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40000")
    vmid = allocate("primera", Inventory())
    assert release(vmid, "primera")
    assert allocate("segunda", Inventory()) == vmid
    assert not release(vmid, "primera")
    with database() as db:
        lease = db.execute("SELECT operation_id, status FROM vmid_leases WHERE vmid=?", (vmid,)).fetchone()
    assert lease == {"operation_id": "segunda", "status": "held"}


def test_heartbeat_respeta_titular_y_cierre_de_operacion():
    initialize()
    with database() as db:
        db.execute(
            """INSERT INTO operations(operation_id, command, status, payload_json, worker_id, created_at)
            VALUES ('op', 'apply', 'running', '{}', 'titular', 'fecha')"""
        )
    assert not heartbeat("otro", "running", "op", require_owned=True)
    assert heartbeat("titular", "running", "op", require_owned=True)
    with database() as db:
        db.execute("UPDATE operations SET status='uncertain' WHERE operation_id='op'")
    assert not heartbeat("titular", "running", "op", require_owned=True)


@pytest.mark.no_database
@pytest.mark.parametrize("data", [None, {"data": []}, [{"name": "sin identidad"}]])
def test_inventario_solo_admite_lista_con_identidad(monkeypatch, data):
    client = object.__new__(ProxmoxClient)
    monkeypatch.setattr(client, "get", lambda *_args, **_kwargs: data)
    with pytest.raises(ProxmoxError, match="PROXMOX_INVENTORY_UNCONFIRMED"):
        client.inventory(uncertain=True)


@pytest.mark.no_database
def test_state_reconciliado_requiere_identidad_de_terraform(tmp_path: Path):
    with pytest.raises(ProxmoxError, match="TERRAFORM_STATE_OWNERSHIP_UNCONFIRMED"):
        main.require_reconciled_terraform_state(str(tmp_path), 40000)


@pytest.mark.no_database
def test_netbox_conserva_direccion_asignada(monkeypatch):
    client = object.__new__(NetBoxClient)
    client.config = SimpleNamespace(expected_prefix="190.0.231.0/24", gateway_tag="reservada")
    record = {
        "id": 7,
        "address": "190.0.231.42/24",
        "description": "iacp-instance=orden",
        "assigned_object_id": 19,
    }
    methods = []

    def request(method, _path, **_kwargs):
        methods.append(method)
        return record

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(NetBoxError, match="IPAM_IDENTITY_MISMATCH"):
        client.release({"netbox_ip_id": 7, "address": record["address"]}, marker=record["description"])
    assert methods == ["GET"]


def test_marcador_fisico_se_exige_para_recursos_nuevos(monkeypatch):
    initialize()
    payload = json.dumps({"physical_identity_tag": "iacp-owned-orden"})
    with database() as db:
        db.execute(
            """INSERT INTO operations(operation_id, command, status, payload_json, created_at)
            VALUES ('orden', 'provision', 'succeeded', ?, 'fecha')""",
            (payload,),
        )
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40000")
    vmid = allocate("orden", Inventory())
    config = {"name": "vm", "tags": "normal"}
    client = SimpleNamespace(vm_config=lambda _node, _vmid: config)
    with pytest.raises(ProxmoxError, match="VM_PHYSICAL_OWNER_MISMATCH"):
        main.assert_physical_identity(client, vmid, "mng01", "vm", "orden")
    config["tags"] = "normal;iacp-owned-orden"
    main.assert_physical_identity(client, vmid, "mng01", "vm", "orden")


@pytest.mark.no_database
def test_workspace_delega_secreto_sin_cargarlo_en_terraform(monkeypatch, tmp_path):
    import sys

    main.save_preboot_expectation(
        str(tmp_path), {"instance": {}, "password_configured_by_worker": True}
    )
    monkeypatch.setenv("TF_VAR_root_password", "solo-valor-sintetico")
    monkeypatch.setattr(main, "heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "read_secret", lambda *_args: pytest.fail("Terraform no debe leer el secreto"))
    code, _, _ = main.run_commands(
        "worker", "op", str(tmp_path),
        [[sys.executable, "-c", "import os; assert 'TF_VAR_root_password' not in os.environ"]],
        secret_path="archivo-no-autorizado-para-terraform",
    )
    assert code == 0


@pytest.mark.no_database
def test_proxmox_no_actualiza_contrasena_con_vm_activa(monkeypatch):
    client = object.__new__(ProxmoxClient)
    monkeypatch.setattr(client, "power_state", lambda *_args: {"power_state": "running"})
    monkeypatch.setattr(client, "put", lambda *_args: pytest.fail("la VM activa no se debe modificar"))
    with pytest.raises(ProxmoxError, match="CLOUDINIT_PASSWORD_VM_ALREADY_RUNNING"):
        client.ensure_cloudinit_password("mng01", 40000, "vm", "root", "valor-sintetico")


@pytest.mark.no_database
def test_proxmox_verifica_configuracion_antes_de_actualizar_contrasena(monkeypatch):
    client = object.__new__(ProxmoxClient)
    monkeypatch.setattr(client, "power_state", lambda *_args: {"power_state": "stopped"})
    observed = iter([
        {"name": "vm", "ciuser": "root", "digest": "revision-config"},
        {"name": "vm", "ciuser": "root", "cipassword": "indicador-configurado"},
    ])
    monkeypatch.setattr(client, "vm_config", lambda *_args: next(observed))
    sent = []
    monkeypatch.setattr(client, "put", lambda _path, parameters: sent.append(parameters))
    client.ensure_cloudinit_password("mng01", 40000, "vm", "root", "valor-sintetico")
    assert sent == [{"cipassword": "valor-sintetico", "digest": "revision-config"}]
