"""Regresiones Core de Guest Initialization y evidencia durable.

Estas pruebas no crean LabPolicy ni usan endpoints históricos. La identidad
del guest se autoriza mediante el vínculo técnico persistido y se observa sin
repetir un guest-exec ya consumido.
"""

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
    Operation,
    TerraformExecution,
)
from iacp.services.guest_identity import (
    GuestClient,
    GuestProgramFailed,
    MODE,
    NETWORK_FILE,
    PROGRAM,
    PROGRAM_SHA256,
    execute_initialization,
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


def test_pending_guest_initialization_blocks_incompatible_power_and_delete(client, auth, guest, monkeypatch):
    monkeypatch.setenv("IACP_TERRAFORM_DELETE_ENABLED", "1")
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    assert accept(client, auth, guest).status_code == 202
    headers = {**auth, "Idempotency-Key": "power-during-init"}
    power = client.post(f"/v1/instances/{guest.id}/power/shutdown", headers=headers, json={})
    deletion = client.delete(f"/v1/instances/{guest.id}", headers=headers)
    assert power.status_code == 409 and power.json()["detail"] == "GUEST_INITIALIZATION_ACTIVE"
    assert deletion.status_code == 409 and deletion.json()["detail"] == "GUEST_INITIALIZATION_ACTIVE"


def test_definitive_guest_failure_is_terminal_and_never_resubmitted(client, auth, guest, sessions):
    operation_id = accept(client, auth, guest).json()["operation_id"]
    fake = FakeGuest(observe_error=GuestProgramFailed("GUEST_INITIALIZATION_PROGRAM_FAILED"))
    execute_initialization(operation_id, sessions, lambda: fake)
    execute_initialization(operation_id, sessions, lambda: fake)
    assert len(fake.calls) == 1
    assert client.get(f"/v1/operations/{operation_id}", headers=auth).json()["status"] == "failed"


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
