"""Regresiones de la recuperación administrativa de Guest Initialization queued."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from iacp.models import (
    BOOTSTRAP_TENANT_ID,
    GuestInitialization,
    Instance,
    Job,
    Operation,
    OperationEvent,
    TechnicalOwnership,
    utcnow,
)
from iacp.services.guest_identity import technical_program_for_mode
from iacp.services.guest_recovery import (
    RecoveryError,
    VERIFY_CONSUMED_EVENT,
    reconcile_consumed_in_session,
)
from tests.power_helpers import managed_instance


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "recover_queued_guest_init", ROOT / "scripts" / "recover_queued_guest_init.py"
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


@pytest.fixture
def queued_guest(sessions):
    instance_id = managed_instance(sessions, BOOTSTRAP_TENANT_ID, vmid=30060,
                                   name="queued-guest-recovery")
    operation_id, job_id = str(uuid4()), str(uuid4())
    with sessions() as session:
        operation = Operation(
            id=operation_id,
            tenant_id=BOOTSTRAP_TENANT_ID,
            instance_id=instance_id,
            kind="instance.network.initialize",
            status="queued",
            idempotency_key="queued-recovery-" + operation_id,
            request_hash="b" * 64,
            requested_by="bootstrap",
        )
        session.add(operation)
        session.add(Job(
            id=job_id,
            tenant_id=BOOTSTRAP_TENANT_ID,
            operation_id=operation_id,
            operation=operation.kind,
            status="queued",
        ))
        session.add(GuestInitialization(
            operation_id=operation_id,
            instance_id=instance_id,
            provisioning_binding="c" * 64,
            mode="ubuntu-2404-lab-identity-v1",
            program_sha256="d" * 64,
            mac_address="bc:24:11:12:34:56",
            status="queued",
        ))
        session.commit()
    return {
        "instance_id": instance_id,
        "operation_id": operation_id,
        "job_id": job_id,
        "vmid": 30060,
        "actor": "operator:test",
        "change_reference": "IACP-RECOVERY-TEST",
    }


def run_recovery(sessions, request, runtime_idle=lambda: {"redis": True}):
    with sessions() as session:
        return recovery.recover_in_session(session, request, runtime_idle=runtime_idle)


def test_queued_never_dispatched_is_cancelled_atomically_and_audited(queued_guest, sessions):
    result = run_recovery(sessions, queued_guest)
    assert result["changed"] is True
    with sessions() as session:
        initialization = session.get(GuestInitialization, queued_guest["operation_id"])
        operation = session.get(Operation, queued_guest["operation_id"])
        job = session.get(Job, queued_guest["job_id"])
        event = session.scalar(select(OperationEvent).where(
            OperationEvent.operation_id == queued_guest["operation_id"],
            OperationEvent.code == recovery.EVENT_CODE,
        ))
        assert initialization.status == operation.status == job.status == "failed"
        assert initialization.started_at is None and initialization.pid is None
        assert operation.error_code == recovery.ERROR_CODE
        assert event is not None and queued_guest["actor"] in event.actor
        assert queued_guest["change_reference"] in event.actor


def test_repeated_recovery_is_idempotent_without_second_runtime_mutation(queued_guest, sessions):
    first = run_recovery(sessions, queued_guest)

    def must_not_run():
        raise AssertionError("runtime check must not run for an already completed cancellation")

    second = run_recovery(sessions, queued_guest, runtime_idle=must_not_run)
    assert first["changed"] is True
    assert second["changed"] is False and second["already_completed"] is True
    with sessions() as session:
        assert len(session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id == queued_guest["operation_id"],
            OperationEvent.code == recovery.EVENT_CODE,
        )).all()) == 1


@pytest.mark.parametrize("change", ["started", "pid", "finished", "result", "consumed", "job"])
def test_recovery_rejects_any_evidence_of_consumption_or_active_job(queued_guest, sessions, change):
    with sessions() as session:
        initialization = session.get(GuestInitialization, queued_guest["operation_id"])
        job = session.get(Job, queued_guest["job_id"])
        if change == "started":
            initialization.started_at = utcnow()
        elif change == "pid":
            initialization.pid = 1234
        elif change == "finished":
            initialization.finished_at = utcnow()
        elif change == "result":
            initialization.result = {"consumed": True}
        elif change == "consumed":
            session.add(OperationEvent(
                operation_id=queued_guest["operation_id"],
                status="running",
                code="guest_initialization_dispatch_consumed",
                actor="worker:iac",
            ))
        else:
            job.status = "running"
        session.commit()

    with pytest.raises(recovery.RecoveryError):
        run_recovery(sessions, queued_guest)
    with sessions() as session:
        assert session.get(GuestInitialization, queued_guest["operation_id"]).status == "queued"


def test_recovery_rejects_other_operation_or_invalid_ownership(queued_guest, sessions):
    with sessions() as session:
        session.add(Operation(
            id=str(uuid4()), tenant_id=BOOTSTRAP_TENANT_ID,
            instance_id=queued_guest["instance_id"], kind="instance.power.start",
            status="running", idempotency_key="power-conflict", request_hash="e" * 64,
            requested_by="operator:test",
        ))
        session.commit()
    with pytest.raises(recovery.RecoveryError, match="INSTANCE_OTHER_OPERATION_ACTIVE"):
        run_recovery(sessions, queued_guest)

    with sessions() as session:
        session.query(Operation).filter(
            Operation.instance_id == queued_guest["instance_id"],
            Operation.kind == "instance.power.start",
        ).delete(synchronize_session=False)
        ownership = session.get(TechnicalOwnership, queued_guest["instance_id"])
        ownership.vmid = 30059
        session.commit()
    with pytest.raises(recovery.RecoveryError, match="TECHNICAL_OWNERSHIP_BINDING_CHANGED"):
        run_recovery(sessions, queued_guest)


def test_runtime_activity_rejects_without_mutating_database(queued_guest, sessions):
    def busy_runtime():
        raise recovery.RecoveryError("WORKER_ACTIVE")

    with pytest.raises(recovery.RecoveryError, match="WORKER_ACTIVE"):
        run_recovery(sessions, queued_guest, runtime_idle=busy_runtime)
    with sessions() as session:
        assert session.get(GuestInitialization, queued_guest["operation_id"]).status == "queued"
        assert session.get(Operation, queued_guest["operation_id"]).status == "queued"
        assert session.get(Job, queued_guest["job_id"]).status == "queued"


def test_commit_failure_rolls_back_every_status_change(queued_guest, sessions, monkeypatch):
    with sessions() as session:
        original_commit = session.commit

        def fail_commit():
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            recovery.recover_in_session(session, queued_guest, runtime_idle=lambda: {"redis": True})
        monkeypatch.setattr(session, "commit", original_commit)

    with sessions() as session:
        assert session.get(GuestInitialization, queued_guest["operation_id"]).status == "queued"
        assert session.get(Operation, queued_guest["operation_id"]).status == "queued"
        assert session.get(Job, queued_guest["job_id"]).status == "queued"
        assert session.scalar(select(OperationEvent.id).where(
            OperationEvent.operation_id == queued_guest["operation_id"],
            OperationEvent.code == recovery.EVENT_CODE,
        )) is None


def test_late_celery_delivery_is_noop_after_cancellation(queued_guest, sessions):
    run_recovery(sessions, queued_guest)
    from iacp.services.guest_identity import execute_initialization

    class ShouldNeverCreateGuestClient:
        def __call__(self):
            raise AssertionError("cancelled delivery must not create a GuestClient")

    execute_initialization(queued_guest["operation_id"], sessions, ShouldNeverCreateGuestClient())
    with sessions() as session:
        assert session.get(GuestInitialization, queued_guest["operation_id"]).status == "failed"


@pytest.fixture
def consumed_technical_guest(sessions):
    instance_id = managed_instance(sessions, BOOTSTRAP_TENANT_ID, vmid=30101,
                                   name="prueba-kvm-2404-003")
    operation_id, job_id = str(uuid4()), str(uuid4())
    mode = "ubuntu-2404-lab-identity-v1"
    with sessions() as session:
        instance = session.get(Instance, instance_id)
        instance.image = "ubuntu-2404"
        instance.network_mode = "dhcp"
        instance.commercial_revision = "provisioning-api-core"
        instance.technical_access_reference = "A" * 43
        operation = Operation(
            id=operation_id, tenant_id=BOOTSTRAP_TENANT_ID,
            instance_id=instance_id, kind="instance.network.initialize",
            status="uncertain", idempotency_key="consumed-" + operation_id,
            request_hash="e" * 64, requested_by="bootstrap",
            error_code="ROOT_ACCESS_CONFIGURATION_MISMATCH",
        )
        session.add(operation)
        session.add(Job(id=job_id, tenant_id=BOOTSTRAP_TENANT_ID,
                        operation_id=operation_id,
                        operation=operation.kind, status="failed"))
        row = GuestInitialization(
            operation_id=operation_id, instance_id=instance_id,
            provisioning_binding=session.get(TechnicalOwnership, instance_id).binding_sha256,
            mode=mode, program_sha256=technical_program_for_mode(mode)[1],
            mac_address="bc:24:11:12:34:56", status="uncertain",
            started_at=utcnow(), pid=1088,
        )
        session.add(row)
        session.add(OperationEvent(
            operation_id=operation_id, status="running",
            code="guest_initialization_dispatch_consumed", actor="worker:iac",
        ))
        session.commit()
    return {
        "instance_id": instance_id, "operation_id": operation_id,
        "job_id": job_id, "vmid": 30101,
        "actor": "operator:test", "change_reference": "IACP-VERIFY-TEST",
        "mode": "verify-consumed",
    }


def test_consumed_uncertain_is_reconciled_by_read_only_root_verification(
        consumed_technical_guest, sessions, monkeypatch):
    password = "A" * 16

    class SecretStore:
        def read(self, reference, tenant_id, instance_id):
            assert reference == "A" * 43
            assert tenant_id == BOOTSTRAP_TENANT_ID
            return password

    class Guest:
        calls = 0

        def observe(self, target, row):
            self.calls += 1
            assert target.vmid == 30101 and row.pid == 1088
            return {
                "instance_id": row.instance_id,
                "mac_address": row.mac_address,
                "mode": row.mode,
                "program_sha256": row.program_sha256,
                "network": {"ip_addresses": ["192.0.2.31"], "status": "observed"},
                "source": "qemu-guest-agent",
            }

    class Verifier:
        calls = 0

        def verify(self, target, instance, *, password, ip_addresses):
            self.calls += 1
            assert password == "A" * 16
            assert ip_addresses == ["192.0.2.31"]
            return {"cloud_init_complete": True, "ssh_authenticated": True}

    guest = Guest()
    verifier = Verifier()
    monkeypatch.setattr("iacp.services.bootstrap_secrets.EphemeralSecrets.configured",
                        classmethod(lambda cls: SecretStore()))
    monkeypatch.setattr("iacp.services.guest_identity._cleanup_technical_access_reference",
                        lambda *_args: None)

    with sessions() as session:
        result = reconcile_consumed_in_session(
            session, consumed_technical_guest, runtime_idle=lambda: {"redis": True},
            guest_factory=lambda: guest, verifier=verifier,
        )

    assert result["status"] == "completed"
    assert guest.calls == verifier.calls == 1
    with sessions() as session:
        row = session.get(GuestInitialization, consumed_technical_guest["operation_id"])
        operation = session.get(Operation, consumed_technical_guest["operation_id"])
        assert row.status == operation.status == "completed"
        assert session.scalar(select(OperationEvent.id).where(
            OperationEvent.operation_id == operation.id,
            OperationEvent.code == VERIFY_CONSUMED_EVENT,
        )) is not None

    class MustNotObserve:
        def observe(self, *_args):
            raise AssertionError("reconciliación idempotente no debe observar de nuevo")

    with sessions() as session:
        replay = reconcile_consumed_in_session(
            session, consumed_technical_guest, runtime_idle=lambda: (_ for _ in ()).throw(
                AssertionError("no debe inspeccionar el runtime")),
            guest_factory=lambda: MustNotObserve(), verifier=verifier,
        )
    assert replay["changed"] is False and replay["already_completed"] is True


def test_consumed_uncertain_never_replays_when_qga_reports_active(
        consumed_technical_guest, sessions, monkeypatch):
    class Guest:
        def observe(self, *_args):
            return None

    with pytest.raises(RecoveryError, match="GUEST_INITIALIZATION_EXEC_ACTIVE"):
        with sessions() as session:
            reconcile_consumed_in_session(
                session, consumed_technical_guest,
                runtime_idle=lambda: {"redis": True}, guest_factory=lambda: Guest(),
            )
    with sessions() as session:
        assert session.get(GuestInitialization,
                           consumed_technical_guest["operation_id"]).status == "uncertain"


def test_api_verify_consumed_mode_uses_the_canonical_read_only_path(
        client, auth, consumed_technical_guest, monkeypatch):
    class SecretStore:
        def read(self, *_args):
            return "A" * 16

    class Guest:
        def observe(self, _target, row):
            return {
                "instance_id": row.instance_id, "mac_address": row.mac_address,
                "mode": row.mode, "program_sha256": row.program_sha256,
                "network": {"ip_addresses": ["192.0.2.32"], "status": "observed"},
            }

    monkeypatch.setattr("iacp.services.bootstrap_secrets.EphemeralSecrets.configured",
                        classmethod(lambda cls: SecretStore()))
    monkeypatch.setattr("iacp.services.guest_identity.GuestClient.configured",
                        classmethod(lambda cls: Guest()))
    monkeypatch.setattr("iacp.services.access.AccessVerifier.verify",
                        lambda *_args, **_kwargs: {
                            "cloud_init_complete": True, "ssh_authenticated": True,
                        })
    monkeypatch.setattr("iacp.services.guest_identity._cleanup_technical_access_reference",
                        lambda *_args: None)
    monkeypatch.setattr("iacp.services.guest_recovery.assert_broker_and_workers_idle",
                        lambda: {"redis": True, "workers": {}})

    path = f"/v1/instances/{consumed_technical_guest['instance_id']}/provisioning/guest-initialization/recover"
    response = client.post(path, headers=auth, json={
        "operation_id": consumed_technical_guest["operation_id"],
        "job_id": consumed_technical_guest["job_id"],
        "mode": "verify-consumed",
    })
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert response.json()["mode"] == "verify-consumed"
