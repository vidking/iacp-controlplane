import hashlib
import json
import secrets
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event, func, select
from sqlalchemy.exc import SQLAlchemyError

from iacp.api.dependencies import get_instance_publisher
from iacp.models import BOOTSTRAP_TENANT_ID, Instance, Job, Operation, OperationEvent, Tenant
try:
    from iacp.services.execution import execute_instance
except ImportError:
    def execute_instance(*args, **kwargs):
        pytest.skip("El worker simulado legacy fue retirado del contrato Core")
from iacp.admin import TenantInput, create_tenant

SPEC = {"name": "test-kvm", "image": "test-image", "vcpus": 2, "memory_mb": 2048, "disk_gb": 20}


@pytest.fixture
def identities(sessions, tmp_path, monkeypatch):
    identities = {}
    registry = []
    with sessions() as session:
        for slug in ("tenant-a", "tenant-b"):
            tenant_id = str(uuid4())
            session.add(Tenant(id=tenant_id, slug=slug, name=slug))
            for role in ("operator", "reader"):
                token = secrets.token_hex(32)
                name = f"{slug}-{role}"
                identities[name] = {"headers": {"Authorization": f"Bearer {token}"}, "tenant_id": tenant_id}
                registry.append({"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "tenant_id": tenant_id,
                                 "subject": name, "role": role})
        session.commit()
    path = tmp_path / "tenant_credentials.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv("IACP_TENANT_CREDENTIALS_FILE", str(path))
    return identities


def create(client, headers, key="create-001", spec=None):
    return client.post("/v1/instances", headers={**headers, "Idempotency-Key": key}, json=spec or SPEC)


def test_creation_is_committed_atomically_before_publish(client, auth, application, sessions):
    def publish(job_id):
        with sessions() as session:
            job = session.get(Job, job_id)
            operation = session.get(Operation, job.operation_id)
            instance = session.get(Instance, operation.instance_id)
            assert job.tenant_id == operation.tenant_id == instance.tenant_id == BOOTSTRAP_TENANT_ID
            assert job.status == operation.status == "queued"
            assert instance.status == "pending"
            assert session.scalar(select(OperationEvent.code)) == "accepted"

    application.dependency_overrides[get_instance_publisher] = lambda: publish
    response = create(client, auth)
    assert response.status_code == 202
    body = response.json()
    assert body["executor"] == "simulated"
    assert client.get(f'/v1/jobs/{body["job_id"]}', headers=auth).json()["operation"] == "instance.create"
    assert client.get(f'/v1/instances/{body["instance_id"]}', headers=auth).json()["vmid"] is None


def test_same_key_replays_once_and_changed_request_conflicts(client, auth, application, sessions):
    publish = Mock()
    application.dependency_overrides[get_instance_publisher] = lambda: publish
    first = create(client, auth)
    assert create(client, auth).json() == first.json()
    assert create(client, auth, spec={**SPEC, "vcpus": 4}).status_code == 409
    assert create(client, auth, key="another-key").status_code == 409  # nombre único por tenant
    publish.assert_called_once_with(first.json()["job_id"])
    with sessions() as session:
        for model in (Instance, Operation, Job, OperationEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_creation_without_key_generates_unique_intention_and_returns_it(client, auth, application, sessions):
    publish = Mock()
    application.dependency_overrides[get_instance_publisher] = lambda: publish
    first = client.post('/v1/instances', headers=auth, json={**SPEC, 'name': 'generated-one'})
    second = client.post('/v1/instances', headers=auth, json={**SPEC, 'name': 'generated-two'})
    assert first.status_code == second.status_code == 202
    first_key, second_key = first.headers.get('Idempotency-Key'), second.headers.get('Idempotency-Key')
    assert first_key and second_key and first_key != second_key
    with sessions() as session:
        for response, key in ((first, first_key), (second, second_key)):
            operation = session.get(Operation, response.json()['operation_id'])
            assert operation.idempotency_key == key


def test_defaults_are_part_of_the_canonical_idempotent_request(client, auth):
    first = create(client, auth, spec={"name": SPEC["name"], "image": SPEC["image"]})
    assert first.status_code == 202
    assert create(client, auth).json() == first.json()


@pytest.mark.parametrize("change", [
    {"tenant_id": "client-controlled"}, {"vmid": 104}, {"node": "unapproved"}, {"executor": "terraform"},
    {"vcpus": 0}, {"vcpus": True}, {"vcpus": "2"}, {"memory_mb": 1}, {"memory_mb": 513}, {"disk_gb": 0},
    {"name": "../unsafe"}, {"image": "../template"},
])
def test_invalid_or_provider_controlled_fields_are_rejected(client, auth, sessions, change):
    assert create(client, auth, spec={**SPEC, **change}).status_code == 422
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Instance)) == 0


@pytest.mark.parametrize("key", ["", "a" * 129, "contains spaces", "../path"])
def test_invalid_idempotency_keys(client, auth, key):
    assert create(client, auth, key=key).status_code == 422


def test_database_failure_rolls_back_all_creation_rows(client, auth, engine, sessions, application):
    publish = Mock()
    application.dependency_overrides[get_instance_publisher] = lambda: publish

    def reject_event(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO operation_events"):
            raise SQLAlchemyError("sensitive database connection")

    sql_event.listen(engine, "before_cursor_execute", reject_event)
    try:
        response = create(client, auth)
    finally:
        sql_event.remove(engine, "before_cursor_execute", reject_event)
    assert response.status_code == 503
    assert "sensitive" not in response.text
    publish.assert_not_called()
    with sessions() as session:
        for model in (Instance, Operation, Job, OperationEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_dispatch_failure_is_traceable_and_retry_does_not_republish(client, auth, application, sessions):
    publish = Mock(side_effect=ConnectionError("secret broker password"))
    application.dependency_overrides[get_instance_publisher] = lambda: publish
    assert create(client, auth).status_code == 503
    replay = create(client, auth).json()
    publish.assert_called_once()
    operation = client.get(f'/v1/operations/{replay["operation_id"]}', headers=auth).json()
    assert operation["status"] == "failed"
    assert operation["error_code"] == "DISPATCH_FAILED"
    assert [item["code"] for item in operation["events"]] == ["accepted", "dispatch_failed"]
    execute_instance(replay["job_id"], sessions)  # entrega tardía no ejecuta un trabajo terminal
    assert client.get(f'/v1/jobs/{replay["job_id"]}', headers=auth).json()["status"] == "failed"
    assert "secret" not in json.dumps(operation)


def test_ambiguous_dispatch_does_not_overwrite_a_completed_instance(client, auth, application, sessions):
    def complete_then_timeout(job_id):
        execute_instance(job_id, sessions)
        raise TimeoutError("ambiguous")

    application.dependency_overrides[get_instance_publisher] = lambda: complete_then_timeout
    assert create(client, auth).status_code == 503
    replay = create(client, auth).json()
    operation = client.get(f'/v1/operations/{replay["operation_id"]}', headers=auth).json()
    assert operation["status"] == "completed"
    assert operation["error_code"] is None
    assert [item["code"] for item in operation["events"]] == ["accepted", "started", "completed"]


def test_simulator_completes_once_without_provider_identifiers(client, auth, sessions):
    accepted = create(client, auth).json()
    execute_instance(accepted["job_id"], sessions)
    response = client.get(f'/v1/instances/{accepted["instance_id"]}', headers=auth)
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["vmid"] is response.json()["node"] is None
    assert response.json()["result"]["simulated"] is True
    execute_instance(accepted["job_id"], sessions, Mock(side_effect=AssertionError("repeated execution")))
    execute_instance(str(uuid4()), sessions)
    assert client.get(f'/v1/instances/{accepted["instance_id"]}', headers=auth).json() == response.json()
    operation = client.get(f'/v1/operations/{accepted["operation_id"]}', headers=auth).json()
    assert [item["status"] for item in operation["events"]] == ["queued", "running", "completed"]


@pytest.mark.parametrize("failure", ["exception", "disabled-executor", "inactive-tenant"])
def test_executor_failures_are_persisted_and_sanitized(client, auth, sessions, monkeypatch, failure):
    accepted = create(client, auth).json()
    executor = None
    if failure == "exception":
        executor = Mock()
        executor.create.side_effect = RuntimeError("secret provider credential")
    elif failure == "disabled-executor":
        monkeypatch.setenv("IACP_EXECUTOR", "terraform")
    else:
        with sessions() as session:
            session.get(Tenant, BOOTSTRAP_TENANT_ID).active = False
            session.commit()
    execute_instance(accepted["job_id"], sessions, executor)
    with sessions() as session:
        job = session.get(Job, accepted["job_id"])
        op = session.get(Operation, accepted["operation_id"])
        assert job.status == op.status == "failed"
        assert op.error_code in {"EXECUTION_FAILED", "EXECUTOR_UNAVAILABLE", "TENANT_INACTIVE"}
        assert "secret" not in job.message
        assert session.get(Instance, accepted["instance_id"]).status == "failed"


def test_tenants_cannot_read_others_inventory_jobs_or_events(client, auth, identities, sessions):
    a = identities["tenant-a-operator"]
    b = identities["tenant-b-operator"]
    first = create(client, a["headers"]).json()
    second = create(client, b["headers"]).json()  # misma clave/nombre en otro tenant, nuevos IDs
    assert first["instance_id"] != second["instance_id"]
    forged = {**b["headers"], "X-Tenant-ID": a["tenant_id"]}
    for resource, id_key in (("instances", "instance_id"), ("jobs", "job_id"), ("operations", "operation_id")):
        path = f'/v1/{resource}/{first[id_key]}'
        assert client.get(path, headers=forged).status_code == 404
        assert client.get(path, headers=auth).status_code == 404  # bootstrap no es administrador global
    rows = client.get("/v1/instances", headers=forged).json()
    assert [row["tenant_id"] for row in rows] == [b["tenant_id"]]
    with sessions() as session:
        session.add(Job(id="legacy", operation="smoke", status="completed"))
        session.commit()
    assert client.get("/v1/jobs/legacy", headers=auth).status_code == 200
    assert client.get("/v1/jobs/legacy", headers=a["headers"]).status_code == 404
    assert client.get("/v1/instances?limit=0", headers=auth).status_code == 422


def test_reader_can_read_but_cannot_create_jobs_or_instances(client, identities):
    operator = identities["tenant-a-operator"]["headers"]
    reader = identities["tenant-a-reader"]["headers"]
    accepted = create(client, operator).json()
    assert create(client, reader, key="reader-attempt").status_code == 403
    assert client.post("/v1/jobs/smoke", headers=reader).status_code == 403
    for resource, key in (("instances", "instance_id"), ("jobs", "job_id"), ("operations", "operation_id")):
        assert client.get(f'/v1/{resource}/{accepted[key]}', headers=reader).status_code == 200


def test_inactive_tenant_and_invalid_registry_fail_closed(client, identities, sessions, monkeypatch, tmp_path):
    identity = identities["tenant-a-operator"]
    with sessions() as session:
        session.get(Tenant, identity["tenant_id"]).active = False
        session.commit()
    assert create(client, identity["headers"]).status_code == 403
    path = tmp_path / "invalid-registry"
    path.write_text('{"token": "secret"}')
    monkeypatch.setenv("IACP_TENANT_CREDENTIALS_FILE", str(path))
    response = create(client, identity["headers"])
    assert response.status_code == 503
    assert "secret" not in response.text


def test_operator_tenant_creation_does_not_create_credentials(sessions):
    with sessions() as session:
        tenant = create_tenant(session, TenantInput(slug="managed", name="Managed tenant"))
        assert session.get(Tenant, tenant.id).active
        assert set(Tenant.__table__.columns.keys()) == {"id", "slug", "name", "active", "created_at"}


@pytest.mark.parametrize("configuration", ["duplicate", "bootstrap-remap", "unknown-tenant", "registry-only"])
def test_registry_identity_safety(client, auth, identities, monkeypatch, configuration):
    import os
    from pathlib import Path

    path = Path(os.environ["IACP_TENANT_CREDENTIALS_FILE"])
    entries = json.loads(path.read_text())
    headers = identities["tenant-a-operator"]["headers"]
    expected = 503
    if configuration == "duplicate":
        entries.append(entries[0])
    elif configuration == "bootstrap-remap":
        token = auth["Authorization"].removeprefix("Bearer ")
        entries[0]["token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
    elif configuration == "unknown-tenant":
        entries[0]["tenant_id"] = str(uuid4())
        expected = 403
    else:
        monkeypatch.delenv("IACP_API_TOKEN_FILE")
        expected = 200
    path.write_text(json.dumps(entries))
    assert client.get("/v1/instances", headers=headers).status_code == expected
