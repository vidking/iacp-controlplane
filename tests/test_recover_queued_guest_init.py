"""Regresiones de la recuperación administrativa de Guest Initialization queued."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from iacp.models import (
    BOOTSTRAP_TENANT_ID,
    GuestInitialization,
    Job,
    Operation,
    OperationEvent,
    TechnicalOwnership,
    utcnow,
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
