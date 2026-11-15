"""Listados sin N+1: misma respuesta que el camino por fila y consultas acotadas."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import api.db as db_module
from api.db import database, initialize
from api.service import instance_row, list_instances, list_operations, operation_row


def _seed(count: int) -> None:
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with database() as db:
        for index in range(count):
            created = (base + timedelta(minutes=index)).isoformat()
            instance_id = f"inst-{index:03d}"
            operation_id = f"op-{index:03d}"
            spec = {"name": f"vm-{index:03d}", "image": "ubuntu-2404", "network_profile": "lab_dhcp"}
            if index % 5 == 0:
                spec.update({"network_profile": "prod_static", "ipv4_cidr": "10.0.0.5/24", "ipv4_gateway": "10.0.0.1"})
            db.execute(
                """
                INSERT INTO operations(operation_id, command, status, payload_json, instance_id, stack, created_at, output)
                VALUES (?, 'provision', ?, '{}', ?, 'mng01', ?, 'salida')
                """,
                (operation_id, "succeeded" if index % 3 else "failed", instance_id, created),
            )
            db.execute(
                """
                INSERT INTO instances(instance_id, operation_id, name, image, vmid, status, spec_json, created_at, updated_at)
                VALUES (?, ?, ?, 'ubuntu-2404', ?, 'ready', ?, ?, ?)
                """,
                (instance_id, operation_id, spec["name"], 40000 + index, json.dumps(spec), created, created),
            )
            for kind in ("operation.created", "operation.started", "terraform.provision.completed"):
                db.execute(
                    "INSERT INTO events(operation_id, kind, status, occurred_at) VALUES (?, ?, 'succeeded', ?)",
                    (operation_id, kind, created),
                )
            if index % 4 == 0:
                db.execute(
                    """
                    INSERT INTO ip_allocations(allocation_id, instance_id, operation_id, profile, prefix_id, marker, status, created_at, address, gateway)
                    VALUES (?, ?, ?, 'prod_static', 7, ?, 'allocated', ?, '10.0.0.9/24', '10.0.0.1')
                    """,
                    (f"alloc-{index}", instance_id, operation_id, f"iacp-instance={instance_id}", created),
                )


@pytest.fixture
def query_counter(monkeypatch):
    counter = {"count": 0}
    original = db_module.DatabaseConnection.execute

    def counting(self, statement, parameters=()):
        counter["count"] += 1
        return original(self, statement, parameters)

    monkeypatch.setattr(db_module.DatabaseConnection, "execute", counting)
    return counter


def test_list_instances_matches_per_row_rendering_and_is_not_n_plus_one(query_counter):
    initialize()
    _seed(60)
    query_counter["count"] = 0
    batched = list_instances(limit=50, offset=0, status="all")
    queries = query_counter["count"]
    assert len(batched["instances"]) == 50
    # Constante: COUNT + página + 3 consultas de prefetch (más SET TIME ZONE al abrir).
    assert queries <= 7, queries

    with database() as db:
        rows = db.execute(
            "SELECT * FROM instances WHERE status != 'deleted' ORDER BY created_at DESC, instance_id DESC LIMIT 50"
        ).fetchall()
    per_row = [instance_row(row) for row in rows]
    assert batched["instances"] == per_row


def test_list_operations_matches_per_row_rendering_and_is_not_n_plus_one(query_counter):
    initialize()
    _seed(60)
    query_counter["count"] = 0
    batched = list_operations(limit=50, offset=0)
    queries = query_counter["count"]
    assert len(batched["operations"]) == 50
    assert queries <= 5, queries

    with database() as db:
        rows = db.execute(
            "SELECT * FROM operations ORDER BY created_at DESC, operation_id DESC LIMIT 50"
        ).fetchall()
    per_row = [operation_row(row) for row in rows]
    assert batched["operations"] == per_row


def test_pagination_is_deterministic_with_identical_timestamps():
    initialize()
    created = datetime(2026, 11, 2, tzinfo=timezone.utc).isoformat()
    with database() as db:
        for index in range(30):
            db.execute(
                """
                INSERT INTO operations(operation_id, command, status, payload_json, created_at)
                VALUES (?, 'plan', 'succeeded', '{}', ?)
                """,
                (f"same-ts-{index:02d}", created),
            )
    seen = []
    for offset in range(0, 30, 7):
        seen.extend(item["operation_id"] for item in list_operations(limit=7, offset=offset)["operations"])
    assert len(seen) == 30
    assert len(set(seen)) == 30
