"""Contrato Core de creación, idempotencia, RBAC y aislamiento por tenant."""

from copy import deepcopy
import hashlib
import json
import os
import secrets
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from iacp.api.dependencies import get_instance_publisher
from iacp.commercial_contract import MINIMAL_EXAMPLE
from iacp.models import BOOTSTRAP_TENANT_ID, Instance, Job, Operation, OperationEvent, Tenant
from iacp.admin import TenantInput, create_tenant
from tests.power_helpers import managed_instance


# Los tests de Terraform conservan esta forma sintética para sus variables
# internas. El helper HTTP de este módulo la traduce al contrato Core.
SPEC = {"name": "test-kvm", "image": "test-image", "vcpus": 2, "memory_mb": 2048, "disk_gb": 20}
CORE_SPEC = {**MINIMAL_EXAMPLE, "name": "test-kvm", "start_on_create": False}


@pytest.fixture(autouse=True)
def core_runtime(monkeypatch, tmp_path):
    """Entorno técnico mínimo para las solicitudes Core de este módulo."""
    monkeypatch.setenv("IACP_INSTANCE_EXECUTOR", "terraform")
    monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "1")
    key = tmp_path / "bootstrap-aes.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    monkeypatch.setenv("IACP_BOOTSTRAP_AES_KEY_FILE", str(key))


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
                subject = f"{slug}-{role}"
                identities[subject] = {"headers": {"Authorization": f"Bearer {token}"},
                                       "tenant_id": tenant_id}
                registry.append({"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                 "tenant_id": tenant_id, "subject": subject, "role": role})
        session.commit()
    path = tmp_path / "tenant_credentials.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv("IACP_TENANT_CREDENTIALS_FILE", str(path))
    return identities


def _core_payload(spec):
    if spec is None:
        return deepcopy(CORE_SPEC)
    if "plan_id" in spec:
        payload = deepcopy(spec)
        payload.setdefault("start_on_create", False)
        return payload
    legacy = dict(spec)
    payload = deepcopy(CORE_SPEC)
    payload["name"] = legacy.get("name", payload["name"])
    image = legacy.get("image")
    if image is not None:
        payload["image_id"] = "ubuntu-2404" if image in {"test-image", "fixture"} else image
    # vcpus=4 is the historical changed-body test; map it to another valid
    # Core plan so idempotency rejects the body instead of schema-validating it.
    if legacy.get("vcpus") == 4 and set(legacy) <= {"name", "image", "vcpus", "memory_mb", "disk_gb"}:
        payload["plan_id"] = "kvm-2"
    for field in ("tenant_id", "vmid", "node", "executor"):
        if field in legacy:
            payload[field] = legacy[field]
    if "vcpus" in legacy and legacy["vcpus"] not in {2, 4}:
        payload["vcpus"] = legacy["vcpus"]
    if "memory_mb" in legacy and legacy["memory_mb"] != 2048:
        payload["memory_mb"] = legacy["memory_mb"]
    if "disk_gb" in legacy and legacy["disk_gb"] != 20:
        payload["disk_gb"] = legacy["disk_gb"]
    return payload


def create(client, headers, key="create-001", spec=None):
    payload = _core_payload(spec or SPEC)
    request_headers = dict(headers)
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return client.post("https://testserver/v1/instances", headers=request_headers, json=payload)


def test_creation_is_committed_atomically_before_publish(client, auth, application, sessions):
    def publish(job_id):
        with sessions() as session:
            job = session.get(Job, job_id)
            operation = session.get(Operation, job.operation_id)
            instance = session.get(Instance, operation.instance_id)
            assert job.tenant_id == operation.tenant_id == instance.tenant_id == BOOTSTRAP_TENANT_ID
            assert job.status == operation.status == "queued"
            assert instance.status == "pending"
            assert session.scalar(select(OperationEvent.code)) == "provisioning_accepted"

    application.dependency_overrides[get_instance_publisher] = lambda: publish
    response = create(client, auth)
    assert response.status_code == 202
    body = response.json()
    assert body["executor"] == body["provisioning_service"] == "provisioning"
    assert client.get(f'/v1/jobs/{body["job_id"]}', headers=auth).json()["operation"] == "instance.plan"
    assert client.get(f'/v1/instances/{body["instance_id"]}', headers=auth).json()["vmid"] is None


def test_same_key_replays_once_and_changed_request_conflicts(client, auth, application, sessions):
    publish = Mock()
    application.dependency_overrides[get_instance_publisher] = lambda: publish
    first = create(client, auth)
    assert first.status_code == 202
    assert create(client, auth).json() == first.json()
    assert create(client, auth, spec={**SPEC, "vcpus": 4}).status_code == 409
    assert create(client, auth, key="another-key").status_code == 409
    publish.assert_called_once_with(first.json()["job_id"])
    with sessions() as session:
        for model in (Instance, Operation, Job, OperationEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_creation_without_key_generates_unique_intention_and_returns_it(client, auth, application, sessions):
    publish = Mock()
    application.dependency_overrides[get_instance_publisher] = lambda: publish
    first_payload = {**CORE_SPEC, "name": "generated-one"}
    second_payload = {**CORE_SPEC, "name": "generated-two"}
    first = client.post("https://testserver/v1/instances", headers=auth, json=first_payload)
    second = client.post("https://testserver/v1/instances", headers=auth, json=second_payload)
    assert first.status_code == second.status_code == 202
    first_key, second_key = first.headers.get("Idempotency-Key"), second.headers.get("Idempotency-Key")
    assert first_key and second_key and first_key != second_key
    with sessions() as session:
        for response, key in ((first, first_key), (second, second_key)):
            operation = session.get(Operation, response.json()["operation_id"])
            assert operation.idempotency_key == key


def test_defaults_are_part_of_the_canonical_idempotent_request(client, auth):
    full = {**CORE_SPEC, "start_on_create": True}
    partial = {k: v for k, v in full.items() if k != "network" and k != "commercial_context"}
    first = client.post("https://testserver/v1/instances", headers={**auth, "Idempotency-Key": "defaults"},
                        json=partial)
    assert first.status_code == 202
    replay = client.post("https://testserver/v1/instances", headers={**auth, "Idempotency-Key": "defaults"},
                         json=full)
    assert replay.json() == first.json()


@pytest.mark.parametrize("change", [
    {"tenant_id": "client-controlled"}, {"vmid": 104}, {"node": "unapproved"},
    {"executor": "terraform"}, {"vcpus": 0}, {"vcpus": True}, {"vcpus": "2"},
    {"memory_mb": 1}, {"memory_mb": 513}, {"disk_gb": 0},
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
    publish = __import__("unittest.mock", fromlist=["Mock"]).Mock()
    application.dependency_overrides[get_instance_publisher] = lambda: publish

    def reject_event(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO operation_events"):
            raise SQLAlchemyError("sensitive database connection")

    from sqlalchemy import event as sql_event
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


def test_dispatch_failure_is_traceable_and_replay_does_not_republish(client, auth, application, sessions):
    publish = Mock(side_effect=ConnectionError("secret broker password"))
    application.dependency_overrides[get_instance_publisher] = lambda: publish
    assert create(client, auth).status_code == 503
    replay = create(client, auth).json()
    publish.assert_called_once()
    operation = client.get(f'/v1/operations/{replay["operation_id"]}', headers=auth).json()
    assert operation["status"] == "queued"
    assert [item["code"] for item in operation["events"]] == ["provisioning_accepted"]
    assert "secret" not in json.dumps(operation)


def test_core_creation_waits_for_terraform_plan(client, auth, sessions):
    accepted = create(client, auth).json()
    instance = client.get(f'/v1/instances/{accepted["instance_id"]}', headers=auth).json()
    assert instance["status"] == "pending" and instance["vmid"] is None
    assert client.get(f'/v1/operations/{accepted["operation_id"]}', headers=auth).json()["status"] == "queued"


def test_tenants_cannot_read_others_inventory_or_operations(client, identities, sessions):
    a = identities["tenant-a-operator"]
    b = identities["tenant-b-operator"]
    first = managed_instance(sessions, a["tenant_id"], vmid=30001, name="tenant-a-kvm")
    second = managed_instance(sessions, b["tenant_id"], vmid=30002, name="tenant-b-kvm")
    for resource, identifier in (("instances", first), ("operations", None)):
        path = f"/v1/{resource}/{identifier or str(uuid4())}"
        assert client.get(path, headers=b["headers"]).status_code == 404
    assert client.get(f"/v1/instances/{first}", headers=a["headers"]).status_code == 200
    assert client.get(f"/v1/instances/{second}", headers=a["headers"]).status_code == 404
    assert {row["tenant_id"] for row in client.get("/v1/instances", headers=a["headers"]).json()} == {a["tenant_id"]}


def test_reader_can_read_but_cannot_create_instances(client, identities, sessions):
    operator = identities["tenant-a-operator"]
    reader = identities["tenant-a-reader"]
    identifier = managed_instance(sessions, operator["tenant_id"], vmid=30003, name="reader-visible")
    assert client.post("https://testserver/v1/instances", headers={**reader["headers"], "Idempotency-Key": "reader"},
                       json=CORE_SPEC).status_code == 403
    assert client.get(f"/v1/instances/{identifier}", headers=reader["headers"]).status_code == 200


def test_inactive_tenant_and_invalid_registry_fail_closed(client, identities, sessions, monkeypatch, tmp_path):
    identity = identities["tenant-a-operator"]
    with sessions() as session:
        session.get(Tenant, identity["tenant_id"]).active = False
        session.commit()
    assert create(client, identity["headers"]).status_code == 403
    path = tmp_path / "invalid-registry"
    path.write_text('{"token": "secret"}')
    monkeypatch.setenv("IACP_TENANT_CREDENTIALS_FILE", str(path))
    response = client.get("/v1/instances", headers=identity["headers"])
    assert response.status_code == 503 and "secret" not in response.text


def test_operator_tenant_creation_does_not_create_credentials(sessions):
    with sessions() as session:
        tenant = create_tenant(session, TenantInput(slug="managed", name="Managed tenant"))
        assert session.get(Tenant, tenant.id).active
        assert set(Tenant.__table__.columns.keys()) == {"id", "slug", "name", "active", "created_at"}
