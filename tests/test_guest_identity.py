"""Regresiones Core de Guest Initialization y evidencia durable.

Estas pruebas no crean LabPolicy ni usan endpoints históricos. La identidad
del guest se autoriza mediante el vínculo técnico persistido y se observa sin
repetir un guest-exec ya consumido.
"""

import base64
import ast
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from iacp.api.dependencies import get_guest_initialization_publisher
from iacp.lifecycle.proxmox import PowerError, Target
from iacp.models import (
    BOOTSTRAP_TENANT_ID,
    GuestInitialization,
    Instance,
    Job,
    Operation,
    OperationEvent,
    TerraformExecution,
)
from iacp.services.guest_identity import (
    GuestClient,
    GuestProgramFailed,
    MODE,
    NETWORK_FILE,
    PROGRAM,
    PROGRAM_SHA256,
    TECHNICAL_ACCESS_PROGRAM,
    configured_mode,
    execute_initialization,
    technical_program_for_mode,
)
from iacp.services.network import after_power
from tests.power_helpers import managed_instance, refresh_technical_ownership


MAC = "bc:24:11:12:34:56"


@pytest.fixture
def guest(sessions, application, monkeypatch):
    """KVM Core sintética con apply físico observado y perfil DHCP técnico."""
    monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "1")
    identifier = managed_instance(sessions, BOOTSTRAP_TENANT_ID, vmid=30001,
                                  name="core-guest-identity")
    with sessions() as session:
        instance = session.get(Instance, identifier)
        execution = session.get(TerraformExecution, identifier)
        instance.image = "ubuntu-2404"
        instance.network_profile = "lab-dhcp"
        instance.network_mode = "dhcp"
        instance.result = {**instance.result, "network": {"mac_address": MAC}}
        execution.allocation = {**execution.allocation, "image": "ubuntu-2404"}
        refresh_technical_ownership(session, identifier)
        session.commit()
    publisher = Mock()
    application.dependency_overrides[get_guest_initialization_publisher] = lambda: publisher
    return SimpleNamespace(
        id=identifier,
        path=f"/v1/instances/{identifier}/network/initialize",
        publisher=publisher,
    )


class FakeGuest:
    def __init__(self, *, submit_error=None, observe_error=None):
        self.calls = []
        self.checks = 0
        self.submit_error = submit_error
        self.observe_error = observe_error

    def check(self, target, mac):
        self.checks += 1

    def ready(self, target):
        return None

    def submit(self, target, identifier, mac):
        self.calls.append((target, identifier, mac))
        if self.submit_error:
            raise self.submit_error
        return 42

    def observe(self, target, row):
        if self.observe_error:
            raise self.observe_error
        return {
            "mode": MODE,
            "instance_id": row.instance_id,
            "mac_address": row.mac_address,
            "program_sha256": PROGRAM_SHA256,
            "network": {"ip_addresses": ["192.0.2.10"], "status": "observed"},
        }


def accept(client, auth, guest, key="identity-one"):
    return client.post(guest.path, headers={**auth, "Idempotency-Key": key}, json={})


def test_core_initializer_is_single_use_and_idempotent(client, auth, guest, sessions):
    response = accept(client, auth, guest)
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation_id"]
    fake = FakeGuest()

    execute_initialization(operation_id, sessions, lambda: fake)
    execute_initialization(operation_id, sessions, lambda: fake)

    assert len(fake.calls) == 1
    assert accept(client, auth, guest).json()["operation_id"] == operation_id
    assert accept(client, auth, guest, key="different").status_code == 409
    result = client.get(f"/v1/operations/{operation_id}", headers=auth).json()
    assert result["normalized_status"] == "completed"
    with sessions() as session:
        instance = session.get(Instance, guest.id)
        row = session.get(GuestInitialization, operation_id)
        assert instance.ip_addresses == ["192.0.2.10"]
        assert row.status == "completed" and row.started_at is not None


def test_administrative_api_cancels_queued_initialization_without_guest_exec(
        client, auth, guest, sessions, monkeypatch):
    accepted = accept(client, auth, guest, key="admin-recovery")
    body = accepted.json()
    monkeypatch.setattr(
        "iacp.services.guest_recovery.assert_broker_and_workers_idle",
        lambda: {"redis": True, "workers": {}},
    )
    path = f"/v1/instances/{guest.id}/provisioning/guest-initialization/recover"
    response = client.post(path, headers=auth, json={
        "operation_id": body["operation_id"], "job_id": body["job_id"],
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["changed"] is True
    assert result["status"] == "failed"
    assert result["error_code"] == "GUEST_INITIALIZATION_CANCELLED_BEFORE_DISPATCH"

    replay = client.post(path, headers=auth, json={
        "operation_id": body["operation_id"], "job_id": body["job_id"],
    })
    assert replay.status_code == 200 and replay.json()["already_completed"] is True
    with sessions() as session:
        initialization = session.get(GuestInitialization, body["operation_id"])
        operation = session.get(Operation, body["operation_id"])
        job = session.get(Job, body["job_id"])
        events = session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id == body["operation_id"],
            OperationEvent.code == "guest_initialization_cancelled_before_dispatch",
        )).all()
        assert initialization.status == operation.status == job.status == "failed"
        assert initialization.started_at is None and initialization.pid is None
        assert len(events) == 1


def test_guest_recovery_requires_administrative_operator(
        client, auth, guest, sessions, tmp_path, monkeypatch):
    accepted = accept(client, auth, guest, key="admin-recovery-rbac")
    body = accepted.json()
    token = "c" * 64
    registry = tmp_path / "credentials.json"
    registry.write_text(json.dumps([{
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "subject": "crm-operator",
        "tenant_id": BOOTSTRAP_TENANT_ID,
        "role": "operator",
        "administration": False,
    }]))
    monkeypatch.setenv("IACP_TENANT_CREDENTIALS_FILE", str(registry))
    monkeypatch.setattr(
        "iacp.services.guest_recovery.assert_broker_and_workers_idle",
        lambda: {"redis": True, "workers": {}},
    )
    path = f"/v1/instances/{guest.id}/provisioning/guest-initialization/recover"
    response = client.post(path, headers={"Authorization": f"Bearer {token}"}, json={
        "operation_id": body["operation_id"], "job_id": body["job_id"],
    })
    assert response.status_code == 403
    with sessions() as session:
        assert session.get(GuestInitialization, body["operation_id"]).status == "queued"


def test_qga_readiness_retries_before_consuming_guest_exec(client, auth, guest, sessions, monkeypatch):
    operation_id = accept(client, auth, guest).json()["operation_id"]

    class FlakyGuest(FakeGuest):
        def check(self, target, mac):
            self.checks += 1
            if self.checks < 3:
                raise PowerError("POWER_PROXMOX_UNAVAILABLE")

    fake = FlakyGuest()
    monkeypatch.setattr("iacp.services.guest_identity.time.sleep", lambda _: None)
    execute_initialization(operation_id, sessions, lambda: fake)
    # Dos reintentos de readiness y una revalidación inmediata antes de
    # consumir el único guest-exec.
    assert fake.checks == 4 and len(fake.calls) == 1
    assert client.get(f"/v1/operations/{operation_id}", headers=auth).json()["normalized_status"] == "completed"


def test_lost_guest_exec_response_reconciles_read_only_without_resubmission(client, auth, guest, sessions):
    operation_id = accept(client, auth, guest).json()["operation_id"]
    fake = FakeGuest(submit_error=PowerError("POWER_PROXMOX_UNAVAILABLE"))
    execute_initialization(operation_id, sessions, lambda: fake)
    assert len(fake.calls) == 1
    assert client.get(f"/v1/operations/{operation_id}", headers=auth).json()["normalized_status"] == "uncertain"

    execute_initialization(operation_id, sessions, lambda: fake)
    assert len(fake.calls) == 1
    assert client.get(f"/v1/operations/{operation_id}", headers=auth).json()["normalized_status"] == "completed"


@pytest.mark.parametrize("change", ["name", "mac", "binding", "tenant"])
def test_changed_identity_fails_closed_before_guest_exec(client, auth, guest, sessions, change):
    operation_id = accept(client, auth, guest).json()["operation_id"]
    with sessions() as session:
        instance = session.get(Instance, guest.id)
        row = session.get(GuestInitialization, operation_id)
        operation = session.get(Operation, operation_id)
        if change == "name":
            instance.name = "foreign-name"
        elif change == "mac":
            row.mac_address = "00:11:22:33:44:55"
        elif change == "binding":
            row.provisioning_binding = "0" * 64
        else:
            operation.tenant_id = "foreign-tenant"
        session.commit()

    fake = FakeGuest()
    execute_initialization(operation_id, sessions, lambda: fake)
    assert fake.calls == []
    with sessions() as session:
        assert session.get(GuestInitialization, operation_id).started_at is None


def test_pending_guest_initialization_blocks_incompatible_power(client, auth, guest, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_DELETE_ENABLED", "1")
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    assert accept(client, auth, guest).status_code == 202
    headers = {**auth, "Idempotency-Key": "power-during-init"}
    power = client.post(f"/v1/instances/{guest.id}/power/shutdown", headers=headers, json={})
    assert power.status_code == 409 and power.json()["detail"] == "GUEST_INITIALIZATION_ACTIVE"


def test_definitive_guest_failure_is_terminal_and_never_resubmitted(client, auth, guest, sessions):
    operation_id = accept(client, auth, guest).json()["operation_id"]
    fake = FakeGuest(observe_error=GuestProgramFailed(
        "GUEST_INITIALIZATION_PROGRAM_FAILED", stage="technical_password", exit_code=23))
    execute_initialization(operation_id, sessions, lambda: fake)
    execute_initialization(operation_id, sessions, lambda: fake)
    assert len(fake.calls) == 1
    assert client.get(f"/v1/operations/{operation_id}", headers=auth).json()["status"] == "failed"
    with sessions() as session:
        assert session.scalar(select(OperationEvent.code).where(
            OperationEvent.operation_id == operation_id,
            OperationEvent.code == 'guest_init_program_failed_technical_password_exit_23'))


def test_disabled_guest_commands_are_terminal_without_consuming_exec(client, auth, guest, sessions):
    operation_id = accept(client, auth, guest).json()["operation_id"]
    fake = FakeGuest()
    fake.require_initializer_capabilities = lambda target: (_ for _ in ()).throw(
        GuestProgramFailed("GUEST_AGENT_COMMANDS_DISABLED"))
    execute_initialization(operation_id, sessions, lambda: fake)
    assert fake.calls == []
    result = client.get(f"/v1/operations/{operation_id}", headers=auth).json()
    assert result["status"] == "failed" and result["error_code"] == "GUEST_AGENT_COMMANDS_DISABLED"


def test_after_start_schedules_guest_initialization_and_network_independently(
        guest, sessions, monkeypatch):
    guest_publisher, network_publisher = Mock(), Mock()
    monkeypatch.setattr("iacp.api.dependencies.get_guest_initialization_publisher", lambda: guest_publisher)
    monkeypatch.setattr("iacp.api.dependencies.get_network_publisher", lambda: network_publisher)
    with sessions() as session:
        instance = session.get(Instance, guest.id)
        operation = Operation(id="power-after-start", tenant_id=instance.tenant_id,
                              instance_id=instance.id, kind="instance.power.start", status="completed",
                              idempotency_key="power-after-start", request_hash="a" * 64,
                              requested_by="bootstrap")
        session.add(operation)
        session.commit()

    after_power("power-after-start", sessions)
    with sessions() as session:
        initialization = session.scalar(select(GuestInitialization).where(
            GuestInitialization.instance_id == guest.id))
        discoveries = session.scalars(select(Operation).where(
            Operation.instance_id == guest.id, Operation.kind == "instance.network.discover")).all()
        assert initialization is not None and initialization.status == "queued"
        assert len(discoveries) == 1
        initialization_id, discovery_id = initialization.operation_id, discoveries[0].id
    guest_publisher.assert_called_once_with(initialization_id)
    network_publisher.assert_called_once_with(discovery_id)


def test_guest_client_accepts_only_durable_marker_and_matching_dhcp_lease():
    machine = "synthetic-machine-id\n"
    network = "synthetic-netplan\n"
    row = SimpleNamespace(instance_id="00000000-0000-4000-8000-000000000001",
                          mac_address=MAC, mode=MODE, program_sha256=PROGRAM_SHA256, pid=42)
    marker = {
        "instance_id": row.instance_id, "mac_address": MAC, "mode": MODE,
        "program_sha256": PROGRAM_SHA256,
        "machine_id_sha256": hashlib.sha256(machine.encode()).hexdigest(),
        "network_sha256": hashlib.sha256(network.encode()).hexdigest(), "ifindex": 2,
    }
    lease_path = "/run/systemd/netif/leases/2"
    files = {
        "/var/lib/iacp/guest-identity.json": json.dumps(marker),
        "/etc/machine-id": machine,
        NETWORK_FILE: network,
        lease_path: "ADDRESS=192.0.2.10\nCLIENTID=01" + MAC.replace(":", "") + "\n",
    }

    def read(method, path, *args):
        assert method == "GET"
        if "/exec-status?" in path:
            raise PowerError("POWER_PROXMOX_UNAVAILABLE")
        if "/file-read?" in path:
            selected = parse_qs(urlsplit(path).query)["file"][0]
            return {"content": files[selected]}
        return {"result": [{"hardware-address": MAC,
                             "ip-addresses": [{"ip-address": "192.0.2.10"}]}]}

    client = GuestClient(SimpleNamespace(_request=read))
    target = Target(30000, "mng01", "core-guest")
    assert client.observe(target, row)["network"]["ip_addresses"] == ["192.0.2.10"]
    files[lease_path] = "ADDRESS=192.0.2.10\nCLIENTID=ff-duplicate-duid\n"
    assert client.observe(target, row) is None
    ast.parse(PROGRAM)


def test_guest_program_failure_persists_only_sanitized_stage_and_exit_code():
    process = {
        'exited': True,
        'exitcode': 23,
        'err-data': base64.b64encode(
            b'IACP_GUEST_FAILURE_STAGE=technical-password\nroot:do-not-persist\n').decode(),
    }
    stage, exit_code = GuestClient._program_failure_diagnostic(process)
    assert stage is None and exit_code == 23

    process['err-data'] = base64.b64encode(
        b'IACP_GUEST_FAILURE_STAGE=technical_password\nroot:do-not-persist\n').decode()
    stage, exit_code = GuestClient._program_failure_diagnostic(process)
    assert stage == 'technical_password' and exit_code == 23
    assert 'do-not-persist' not in str((stage, exit_code))


def test_technical_initializer_uses_qga_stdin_for_access_without_commercial_marker():
    calls = []

    def request(method, path, parameters=None):
        calls.append((method, path, parameters))
        return {'pid': 42}

    client = GuestClient(SimpleNamespace(_request=request))
    program, digest = technical_program_for_mode(MODE)
    assert digest != PROGRAM_SHA256
    assert 'commercial-bootstrap.json' not in program
    ast.parse(TECHNICAL_ACCESS_PROGRAM)

    client.submit_technical_access(
        Target(30000, 'mng01', 'core-guest'),
        '00000000-0000-4000-8000-000000000001',
        MAC,
        MODE,
        'SENUELO-TECNICO-NO-PERSISTIR-2026',
        [],
    )
    parameters = calls[0][2]
    command_values = [value for key, value in parameters if key == 'command']
    input_values = [value for key, value in parameters if key == 'input-data']
    assert len(input_values) == 1
    assert 'SENUELO-TECNICO-NO-PERSISTIR-2026' in input_values[0]
    assert 'SENUELO-TECNICO-NO-PERSISTIR-2026' not in command_values


def test_technical_initializer_verifies_root_and_cleans_ephemeral_reference(
        client, auth, guest, sessions, monkeypatch):
    from iacp.services.bootstrap_secrets import EphemeralSecrets
    from iacp.services.guest_identity import technical_program_for_mode

    class MemoryRedis:
        def __init__(self):
            self.values = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

        def get(self, key):
            return self.values.get(key)

        def delete(self, key):
            self.values.pop(key, None)

    store = EphemeralSecrets(MemoryRedis(), b'k' * 32, 900)
    password = 'SENUELO-TECNICO-NO-PERSISTIR-2026'
    with sessions() as session:
        instance = session.get(Instance, guest.id)
        instance.technical_access_reference = store.put(
            instance.tenant_id, instance.id, password)
        instance.technical_access_public_keys = []
        instance.commercial_revision = 'provisioning-api-core'
        session.commit()

    operation_id = accept(client, auth, guest, key='technical-access').json()['operation_id']
    technical_digest = technical_program_for_mode(MODE)[1]

    class TechnicalGuest(FakeGuest):
        def submit_technical_access(self, target, identifier, mac, mode, value, keys):
            self.calls.append((target, identifier, mac, mode, value, keys))
            assert mode == MODE and value == password and keys == []
            return 42

        def observe(self, target, row):
            return {
                'mode': MODE,
                'instance_id': row.instance_id,
                'mac_address': row.mac_address,
                'program_sha256': technical_digest,
                'network': {'ip_addresses': ['192.0.2.10'], 'status': 'observed'},
            }

    class Verified:
        def verify(self, target, instance, *, password, ip_addresses):
            assert password == 'SENUELO-TECNICO-NO-PERSISTIR-2026'
            assert ip_addresses == ['192.0.2.10']
            return {'status': 'completed', 'cloud_init_complete': True,
                    'ssh_authenticated': True}

    monkeypatch.setattr(EphemeralSecrets, 'configured', classmethod(lambda cls: store))
    monkeypatch.setattr('iacp.services.access.AccessVerifier', Verified)
    fake = TechnicalGuest()
    execute_initialization(operation_id, sessions, lambda: fake)

    assert len(fake.calls) == 1
    with sessions() as session:
        instance = session.get(Instance, guest.id)
        assert instance.access_verification['ssh_authenticated'] is True
        assert instance.technical_access_reference is None
        assert configured_mode(session, instance) == MODE
        assert session.get(GuestInitialization, operation_id).status == 'completed'
        assert not session.scalar(select(Operation).where(
            Operation.instance_id == guest.id,
            Operation.kind == 'instance.access.bootstrap'))
