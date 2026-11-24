from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from api import service
from api.db import database, initialize, prune_history
from api.models import Command, InstanceSpec, OperationRequest


def create_plan(name: str) -> dict:
    return service.create_operation(OperationRequest(
        command=Command.plan,
        plan_id="kvm-1",
        spec=InstanceSpec(name=name),
    ))


def mark_planned(record: dict) -> None:
    with database() as db:
        db.execute("UPDATE operations SET status='succeeded' WHERE operation_id=?", (record["operation_id"],))
        db.execute("UPDATE instances SET status='planned' WHERE instance_id=?", (record["instance_id"],))


def test_plan_canonico_de_la_instancia():
    initialize()
    first = create_plan("plan-canonico")
    other = create_plan("plan-independiente")
    mark_planned(first)
    mark_planned(other)

    with pytest.raises(HTTPException, match="no pertenece") as failure:
        service.create_provision(first["instance_id"], other["operation_id"])
    assert failure.value.status_code == 409

    queued = service.create_provision(first["instance_id"])
    assert queued["instance_id"] == first["instance_id"]
    with database() as db:
        operation = db.execute(
            "SELECT instance_id, plan_operation_id FROM operations WHERE operation_id=?",
            (queued["operation_id"],),
        ).fetchone()
    assert operation == {"instance_id": first["instance_id"], "plan_operation_id": first["operation_id"]}


def test_admision_concurrente_de_planes_preserva_unicidad():
    initialize()

    def admit():
        try:
            return create_plan("plan-concurrente")["instance_id"]
        except HTTPException as failure:
            assert failure.status_code == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(lambda _: admit(), range(2)))
    assert sum(value is not None for value in accepted) == 1
    with database() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM operations").fetchone()["n"] == 1
        assert db.execute("SELECT COUNT(*) AS n FROM instances").fetchone()["n"] == 1
        assert db.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 1


def test_error_al_persistir_evento_revierte_toda_la_admision(monkeypatch):
    initialize()

    def fail(*_args, **_kwargs):
        raise RuntimeError("fallo simulado al registrar evento")

    monkeypatch.setattr(service, "emit", fail)
    with pytest.raises(RuntimeError, match="fallo simulado"):
        create_plan("operacion-transaccional")
    with database() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM operations").fetchone()["n"] == 0
        assert db.execute("SELECT COUNT(*) AS n FROM instances").fetchone()["n"] == 0


def test_orden_de_destruccion_exige_apagado_y_plan_vigente():
    initialize()
    plan = create_plan("validacion-destroy")
    mark_planned(plan)
    with database() as db:
        db.execute(
            "UPDATE instances SET status='ready', power_state='running' WHERE instance_id=?",
            (plan["instance_id"],),
        )
    request = OperationRequest(command=Command.destroy, instance_id=plan["instance_id"])
    with pytest.raises(HTTPException, match="instancia está apagada") as failure:
        service.create_operation(request)
    assert failure.value.status_code == 409

    with database() as db:
        db.execute(
            "UPDATE instances SET power_state='stopped' WHERE instance_id=?",
            (plan["instance_id"],),
        )
    queued = service.create_operation(request)
    assert queued["action"] == "destroy"
    with database() as db:
        actual = db.execute(
            "SELECT plan_operation_id FROM operations WHERE operation_id=?",
            (queued["operation_id"],),
        ).fetchone()
    assert actual["plan_operation_id"] == plan["operation_id"]


def test_retencion_conserva_fuentes_y_recuperaciones_pendientes(monkeypatch):
    initialize()
    plan = create_plan("retencion-plan")
    old = "2020-01-01T00:00:00Z"
    with database() as db:
        db.execute(
            "UPDATE operations SET status='succeeded', finished_at=? WHERE operation_id=?",
            (old, plan["operation_id"]),
        )
        for operation_id, status in (("recuperacion", "uncertain"), ("terminal", "failed")):
            db.execute(
                """
                INSERT INTO operations(operation_id, command, status, payload_json, created_at, finished_at)
                VALUES (?, 'reconcile', ?, '{}', ?, ?)
                """,
                (operation_id, status, old, old),
            )
    monkeypatch.setenv("IACP_RETENTION_DAYS", "30")
    assert prune_history()["operations"] == 1
    with database() as db:
        remaining = db.execute("SELECT operation_id FROM operations").fetchall()
    assert {row["operation_id"] for row in remaining} == {"recuperacion", plan["operation_id"]}


def test_hmac_idempotente_acepta_y_migra_huella_legada():
    initialize()
    scope, key = "POST:prueba", "compatibilidad"
    body = {"nombre": "valor-de-prueba"}

    def builder(_db):
        return {"creado": True}, None

    def retry():
        return service._idempotent_mutation(scope=scope, idempotency_key=key, body=body, builder=builder)

    first = retry()
    with database() as db:
        actual = db.execute("SELECT request_hash FROM idempotency_keys").fetchone()
        assert actual["request_hash"].startswith("hmac-sha256-v1:")
        db.execute("UPDATE idempotency_keys SET request_hash=?", (service._body_fingerprint(body),))

    assert retry() == first
    assert service.migrate_idempotency_fingerprints(limit=1) == 1
    assert service.migrate_idempotency_fingerprints(limit=1) == 0
    assert retry() == first
