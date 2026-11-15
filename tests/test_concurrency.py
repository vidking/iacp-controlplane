"""Concurrencia: idempotencia, nombres duplicados y VMID con 10, 50 y 100 solicitudes."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.db import database, initialize
from api.main import app
from test_api import public_body
from worker.allocation import allocate


class _EmptyProxmox:
    def inventory(self, *, uncertain=False):
        return []


@pytest.mark.parametrize("concurrency", [10, 50, 100])
def test_same_idempotency_key_creates_exactly_one_operation(concurrency):
    with TestClient(app) as client:
        def create(_):
            return client.post(
                "/v1/instances",
                headers={"Idempotency-Key": f"concurrent-key-{concurrency}"},
                json=public_body(f"concurrent-idem-{concurrency}"),
            )

        with ThreadPoolExecutor(max_workers=min(concurrency, 32)) as executor:
            responses = list(executor.map(create, range(concurrency)))
    assert {response.status_code for response in responses} == {202}
    operation_ids = {response.json()["operation_id"] for response in responses}
    assert len(operation_ids) == 1
    with database() as db:
        operations = db.execute(
            "SELECT COUNT(*) AS n FROM operations WHERE command='provision'"
        ).fetchone()["n"]
        instances = db.execute(
            "SELECT COUNT(*) AS n FROM instances WHERE name=?", (f"concurrent-idem-{concurrency}",)
        ).fetchone()["n"]
    assert operations == 1
    assert instances == 1


@pytest.mark.parametrize("concurrency", [10, 50, 100])
def test_duplicate_instance_names_are_accepted_only_once(concurrency):
    name = f"concurrent-name-{concurrency}"
    with TestClient(app) as client:
        def create(index):
            return client.post(
                "/v1/instances",
                headers={"Idempotency-Key": f"name-{concurrency}-{index}"},
                json=public_body(name),
            )

        with ThreadPoolExecutor(max_workers=min(concurrency, 32)) as executor:
            responses = list(executor.map(create, range(concurrency)))
    codes = sorted(response.status_code for response in responses)
    assert codes.count(202) == 1
    assert codes.count(409) == concurrency - 1
    with database() as db:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM instances WHERE name=? AND status <> 'deleted'", (name,)
        ).fetchone()["n"]
    assert count == 1


@pytest.mark.parametrize("concurrency", [10, 50, 100])
def test_concurrent_vmid_allocation_never_duplicates(monkeypatch, concurrency):
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", str(40000 + concurrency - 1))
    initialize()
    created = datetime.now(timezone.utc).isoformat()
    with database() as db:
        for index in range(concurrency):
            db.execute(
                """
                INSERT INTO operations(operation_id, command, status, payload_json, instance_id, created_at)
                VALUES (?, 'provision', 'running', '{}', ?, ?)
                """,
                (f"alloc-{index}", f"alloc-instance-{index}", created),
            )
    client = _EmptyProxmox()
    with ThreadPoolExecutor(max_workers=min(concurrency, 32)) as executor:
        vmids = list(executor.map(lambda index: allocate(f"alloc-{index}", client), range(concurrency)))
    assert len(set(vmids)) == concurrency
    assert set(vmids) == set(range(40000, 40000 + concurrency))
    # El rango está agotado: la siguiente asignación falla sin duplicar.
    with pytest.raises(RuntimeError, match="VMID_RANGE_EXHAUSTED"):
        allocate("alloc-extra", client)


def test_held_vmid_lease_is_never_overwritten(monkeypatch):
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40000")
    initialize()
    created = datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute(
            "INSERT INTO vmid_leases(vmid, operation_id, status, created_at) VALUES (40000, 'owner', 'held', ?)",
            (created,),
        )
    with pytest.raises(RuntimeError, match="VMID_RANGE_EXHAUSTED"):
        allocate("intruder", _EmptyProxmox())
    with database() as db:
        owner = db.execute("SELECT operation_id FROM vmid_leases WHERE vmid=40000").fetchone()["operation_id"]
    assert owner == "owner"


def test_vmid_present_in_proxmox_is_not_reused_even_if_lease_was_released(monkeypatch):
    monkeypatch.setenv("IACP_VMID_MIN", "40000")
    monkeypatch.setenv("IACP_VMID_MAX", "40001")
    initialize()
    created = datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute(
            "INSERT INTO vmid_leases(vmid, operation_id, status, created_at, released_at) VALUES (40000, 'old', 'released', ?, ?)",
            (created, created),
        )

    class OrphanProxmox:
        def inventory(self, *, uncertain=False):
            return [{"vmid": 40000, "name": "huerfana", "node": "mng01"}]

    assert allocate("new-op", OrphanProxmox()) == 40001
