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


def test_reserva_se_revalida_bajo_cerrojo_tras_inventario():
    initialize()
    with database() as db:
        db.execute(
            """INSERT INTO operations(operation_id, command, status, payload_json, worker_id, created_at)
            VALUES ('op', 'provision', 'running', '{}', 'worker', 'fecha')"""
        )

    class InventarioConOperacionRecuperada:
        def inventory(self):
            with database() as db:
                db.execute("UPDATE operations SET status='uncertain' WHERE operation_id='op'")
            return []

    with pytest.raises(main.OperationFencedError):
        allocate(
            "op", InventarioConOperacionRecuperada(),
            check_owner=lambda db: main.assert_active_operation_owned(db, "worker", "op"),
        )
    with database() as db:
        assert db.execute("SELECT 1 FROM vmid_leases").fetchone() is None
        assert db.execute("SELECT vmid FROM operations WHERE operation_id='op'").fetchone()["vmid"] is None


@pytest.mark.no_database
def test_cerrojo_netbox_ocupado_no_implica_mutacion(monkeypatch):
    from contextlib import contextmanager

    from psycopg.errors import LockNotAvailable

    from api import db as db_module

    class CerrojoOcupado:
        def execute(self, statement, *_args):
            if "pg_advisory_xact_lock" in statement:
                raise LockNotAvailable("ocupado")

    @contextmanager
    def database_fake():
        yield CerrojoOcupado()

    monkeypatch.setattr(db_module, "database", database_fake)
    client = object.__new__(NetBoxClient)
    client.config = SimpleNamespace(expected_prefix="190.0.231.0/24")
    monkeypatch.setattr(client, "_allocate", lambda **_kwargs: pytest.fail("NetBox aún no debe recibir solicitudes"))
    with pytest.raises(NetBoxError) as failure:
        client.allocate()
    assert failure.value.code == "IPAM_ALLOCATION_BUSY"
    assert not failure.value.uncertain


def test_cierre_fuente_revierte_instancia_y_reserva_si_falla_evento(monkeypatch):
    initialize()
    with database() as db:
        db.execute(
            """INSERT INTO operations(operation_id, command, status, payload_json, created_at)
            VALUES ('op', 'provision', 'uncertain', '{}', 'fecha')"""
        )
        db.execute(
            """INSERT INTO instances(instance_id, operation_id, name, image, vmid, status,
            spec_json, created_at, updated_at)
            VALUES ('inst', 'op', 'vm', 'image', 40000, 'uncertain', '{}', 'fecha', 'fecha')"""
        )
        db.execute(
            """INSERT INTO vmid_leases(vmid, operation_id, status, created_at)
            VALUES (40000, 'op', 'held', 'fecha')"""
        )

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("sin registro de evento")

    monkeypatch.setattr(main, "emit", fail_event)
    with pytest.raises(RuntimeError, match="sin registro de evento"):
        main.close_source_after_reconciliation(
            {"operation_id": "op", "command": "provision"}, "inst", 40000, "mng01", {},
            succeeded=False, detail="VM ausente",
        )
    with database() as db:
        assert db.execute("SELECT status FROM operations WHERE operation_id='op'").fetchone()["status"] == "uncertain"
        assert db.execute("SELECT status FROM instances WHERE instance_id='inst'").fetchone()["status"] == "uncertain"
        assert db.execute("SELECT status FROM vmid_leases WHERE vmid=40000").fetchone()["status"] == "held"


@pytest.mark.no_database
def test_terminacion_senaliza_grupo_aunque_lider_ya_salio(monkeypatch):
    signals = []
    monkeypatch.setattr(main.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    process = SimpleNamespace(pid=42, wait=lambda *, timeout: 0)
    main._terminate_process_group(process)
    assert signals == [(42, main.signal.SIGTERM), (42, main.signal.SIGKILL)]
