"""Regresiones del cierre histórico previo a ``ssh.connect()``."""

import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from iacp.models import (
    BOOTSTRAP_TENANT_ID,
    CommercialIntent,
    Job,
    Operation,
    OperationEvent,
    TechnicalOwnership,
    TerraformExecution,
    utcnow,
)
from iacp.services.bootstrap_secrets import BootstrapSecretError
from iacp.services import commercial_bootstrap_recovery as recovery
from tests.power_helpers import managed_instance
from tests.test_commercial_admission import store


def evidence_for(request):
    evidence = {
        "evidence_version": "iacp.bootstrap-preconnect.v1",
        "instance_id": request["instance_id"],
        "operation_id": request["operation_id"],
        "job_id": request["job_id"],
        "vmid": request["vmid"],
        "historical_runtime_sha": recovery.HISTORICAL_RUNTIME_SHA,
        "secure_bootstrap_source_sha256": recovery.SECURE_BOOTSTRAP_SHA256,
        "commercial_bootstrap_source_sha256": recovery.COMMERCIAL_BOOTSTRAP_SHA256,
        "inspection_mode": "read-only",
        "source": "persisted-runtime-evidence",
        "physical_identity_verified": True,
        "ssh_connect_attempted": False,
        "credential_delivery_started": False,
        "preconnect_sequence": list(recovery.PRECONNECT_SEQUENCE),
        "qga": {
            "path": recovery.HOST_KEY_PATH,
            "error_class": "permission_denied",
            "error_code": "BOOTSTRAP_HOST_KEY_CHANNEL_UNAVAILABLE",
        },
        "marker": {"path": recovery.MARKER_PATH, "status": "absent"},
        "evidence_reference": "evidence/30093-preconnect.json",
    }
    evidence["evidence_sha256"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return evidence


@pytest.fixture
def uncertain_bootstrap(sessions, store):
    instance_id = managed_instance(
        sessions, BOOTSTRAP_TENANT_ID, vmid=30093, name="historical-alma9-30093"
    )
    operation_id, job_id = str(uuid4()), str(uuid4())
    reference = store.put(BOOTSTRAP_TENANT_ID, instance_id, "never-read-test-secret")
    with sessions() as session:
        # El fixture helper devuelve solo el UUID; cargar el modelo explícitamente
        # evita depender de relaciones de ORM no necesarias para esta prueba.
        from iacp.models import Instance
        instance = session.get(Instance, instance_id)
        instance.image = "almalinux-9"
        instance.commercial_revision = "historical"
        session.add(CommercialIntent(
            instance_id=instance_id,
            revision="historical",
            plan_id="kvm-1",
            image_id="almalinux-9",
            catalog_sha256="a" * 64,
            request_fingerprint="b" * 64,
            specification={"profile_snapshot": {"settings": {"cloudinit_username": "root"}}},
            secret_reference=reference,
            bootstrap_status="uncertain",
            bootstrap_started_at=utcnow(),
            error_code=recovery.PRIOR_ERROR_CODE,
        ))
        operation = Operation(
            id=operation_id,
            tenant_id=BOOTSTRAP_TENANT_ID,
            instance_id=instance_id,
            kind=recovery.OPERATION_KIND,
            status="uncertain",
            idempotency_key="historical-bootstrap-" + operation_id,
            request_hash="c" * 64,
            requested_by="bootstrap",
            error_code=recovery.PRIOR_ERROR_CODE,
        )
        session.add(operation)
        session.add(Job(
            id=job_id,
            tenant_id=BOOTSTRAP_TENANT_ID,
            operation_id=operation_id,
            operation=recovery.OPERATION_KIND,
            status="failed",
        ))
        session.add_all([
            OperationEvent(
                operation_id=operation_id,
                status="running",
                code="commercial_bootstrap_dispatch_consumed",
                actor="worker:iac",
            ),
            OperationEvent(
                operation_id=operation_id,
                status="uncertain",
                code="commercial_bootstrap_uncertain",
                actor="worker:iac",
            ),
        ])
        session.commit()
    request = {
        "instance_id": instance_id,
        "operation_id": operation_id,
        "job_id": job_id,
        "vmid": 30093,
        "actor": "operator:test",
        "change_reference": "IACP-30093-PRECONNECT",
    }
    request["evidence"] = evidence_for(request)
    return request, store


def run_recovery(sessions, request, store, runtime_idle=lambda: {"redis": True}):
    with sessions() as session:
        return recovery.recover_in_session(
            session, request, runtime_idle=runtime_idle, store=store
        )


def deleted_absence_request(sessions, request):
    """Sella la evidencia pública mínima de una ausencia ya observada."""
    with sessions() as session:
        from iacp.models import Instance
        instance = session.get(Instance, request["instance_id"])
        ownership = session.get(recovery.TechnicalOwnership, request["instance_id"])
        execution = session.get(recovery.TerraformExecution, request["instance_id"])
        instance.status = "deleted"
        evidence = {
            "evidence_version": recovery.DELETED_ABSENCE_EVIDENCE_VERSION,
            "instance_id": request["instance_id"],
            "operation_id": request["operation_id"],
            "job_id": request["job_id"],
            "vmid": request["vmid"],
            "historical_runtime_sha": recovery.HISTORICAL_RUNTIME_SHA,
            "secure_bootstrap_source_sha256": recovery.SECURE_BOOTSTRAP_SHA256,
            "commercial_bootstrap_source_sha256": recovery.COMMERCIAL_BOOTSTRAP_SHA256,
            "inspection_mode": "read-only",
            "source": "runtime-absence-observation",
            "physical_identity_verified": True,
            "instance_status": "deleted",
            "operation_kind": recovery.OPERATION_KIND,
            "operation_status": "uncertain",
            "operation_error_code": recovery.PRIOR_ERROR_CODE,
            "job_status": "failed",
            "source_revision": "c" * 40,
            "bootstrap_executor_sha256": "d" * 64,
            "tenant_id": instance.tenant_id,
            "node": instance.node,
            "name": instance.name,
            "state_key_sha256": hashlib.sha256(execution.state_key.encode()).hexdigest(),
            "ownership_binding_sha256": ownership.binding_sha256,
            "proxmox_present": False,
            "state_resource_present": False,
            "state_resource_id": None,
            "state_lock_absent": True,
            "redelivery_mode": "observe-only-fenced",
            "state_snapshot": {
                "version_id": "version-1",
                "lineage": "lineage-1",
                "serial": 7,
                "sha256": "a" * 64,
                "content_sha256": "b" * 64,
            },
            "evidence_reference": "evidence/30093-deleted-absence.json",
        }
        evidence["evidence_sha256"] = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        request["evidence"] = evidence
        session.commit()
    return request


@pytest.fixture
def deleted_absence_bootstrap(sessions, uncertain_bootstrap):
    request, store = uncertain_bootstrap
    return deleted_absence_request(sessions, request), store


def test_consumed_preconnect_recovery_closes_exact_case_without_delivery(
    sessions, uncertain_bootstrap
):
    request, store = uncertain_bootstrap
    result = run_recovery(sessions, request, store)
    assert result["changed"] is True
    assert result["error_code"] == recovery.ERROR_CODE
    assert store.redis.values == {}
    with sessions() as session:
        intent = session.get(CommercialIntent, request["instance_id"])
        operation = session.get(Operation, request["operation_id"])
        job = session.get(Job, request["job_id"])
        assert intent.bootstrap_status == "failed"
        assert intent.bootstrap_started_at is not None
        assert intent.bootstrap_completed_at is None
        assert intent.secret_reference is None
        assert operation.status == job.status == "failed"
        assert operation.error_code == recovery.ERROR_CODE
        codes = [row.code for row in session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id == request["operation_id"]
        )).all()]
        assert "commercial_bootstrap_dispatch_consumed" in codes
        assert recovery.EVENT_CODE in codes
        assert "commercial_bootstrap_completed" not in codes


def test_deleted_absence_recovery_closes_exact_historical_fence(
    sessions, deleted_absence_bootstrap
):
    request, store = deleted_absence_bootstrap
    result = run_recovery(sessions, request, store)
    assert result["changed"] is True
    assert result["error_code"] == recovery.ERROR_CODE
    with sessions() as session:
        from iacp.models import Instance
        instance = session.get(Instance, request["instance_id"])
        operation = session.get(Operation, request["operation_id"])
        assert instance.status == "deleted"
        assert operation.status == "failed"
        assert operation.error_code == recovery.ERROR_CODE


def test_deleted_absence_recovery_is_idempotent(
    sessions, deleted_absence_bootstrap
):
    request, store = deleted_absence_bootstrap
    first = run_recovery(sessions, request, store)

    def must_not_run():
        raise AssertionError("No debe repetirse una comprobación de runtime")

    second = run_recovery(sessions, request, store, runtime_idle=must_not_run)
    assert first["changed"] is True
    assert second["changed"] is False
    assert second["already_completed"] is True


@pytest.mark.parametrize(
    "change",
    [
        {"instance_status": "ready"},
        {"proxmox_present": True},
        {"state_resource_present": True},
        {"state_lock_absent": False},
        {"operation_status": "running"},
        {"job_status": "running"},
        {"redelivery_mode": "replay-allowed"},
    ],
)
def test_deleted_absence_proof_mismatch_fails_closed(
    sessions, deleted_absence_bootstrap, change
):
    request, store = deleted_absence_bootstrap
    request["evidence"].update(change)
    with pytest.raises(recovery.RecoveryError):
        run_recovery(sessions, request, store)
    with sessions() as session:
        assert session.get(Operation, request["operation_id"]).status == "uncertain"
        from iacp.models import Instance
        assert session.get(Instance, request["instance_id"]).status == "deleted"


def test_recovery_is_idempotent_and_does_not_reinspect_runtime(
    sessions, uncertain_bootstrap
):
    request, store = uncertain_bootstrap
    first = run_recovery(sessions, request, store)

    def must_not_run():
        raise AssertionError("No debe repetirse una comprobación de runtime")

    second = run_recovery(sessions, request, store, runtime_idle=must_not_run)
    assert first["changed"] is True
    assert second["changed"] is False
    assert second["already_completed"] is True
    with sessions() as session:
        assert len(session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id == request["operation_id"],
            OperationEvent.code == recovery.EVENT_CODE,
        )).all()) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"ssh_connect_attempted": True},
        {"credential_delivery_started": True},
        {"historical_runtime_sha": "f" * 40},
        {"marker": {"path": recovery.MARKER_PATH, "status": "present"}},
        {"qga": {"path": recovery.HOST_KEY_PATH, "error_class": "transport"}},
    ],
)
def test_evidence_mismatch_fails_closed_without_mutating(
    sessions, uncertain_bootstrap, change
):
    request, store = uncertain_bootstrap
    request["evidence"].update(change)
    with pytest.raises(recovery.RecoveryError):
        run_recovery(sessions, request, store)
    with sessions() as session:
        assert session.get(Operation, request["operation_id"]).status == "uncertain"
        assert session.get(CommercialIntent, request["instance_id"]).bootstrap_status == "uncertain"


@pytest.mark.parametrize("change", ["started", "pid", "completed", "active"])
def test_state_or_concurrency_mismatch_fails_closed(sessions, uncertain_bootstrap, change):
    request, store = uncertain_bootstrap
    with sessions() as session:
        if change == "started":
            session.get(CommercialIntent, request["instance_id"]).bootstrap_started_at = None
        elif change == "pid":
            session.get(Operation, request["operation_id"]).error_code = "OTHER_ERROR"
        elif change == "completed":
            session.add(OperationEvent(
                operation_id=request["operation_id"], status="completed",
                code="commercial_bootstrap_completed", actor="worker:iac",
            ))
        else:
            session.add(Operation(
                id=str(uuid4()), tenant_id=BOOTSTRAP_TENANT_ID,
                instance_id=request["instance_id"], kind="instance.power.start",
                status="running", idempotency_key="active-" + str(uuid4()),
                request_hash="d" * 64, requested_by="worker",
            ))
        session.commit()
    with pytest.raises(recovery.RecoveryError):
        run_recovery(sessions, request, store)
def test_rollback_keeps_uncertain_fence_if_commit_fails(
    sessions, uncertain_bootstrap, monkeypatch
):
    request, store = uncertain_bootstrap
    with sessions() as session:
        original_commit = session.commit

        def fail_commit():
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            recovery.recover_in_session(
                session, request, runtime_idle=lambda: {"redis": True}, store=store
            )
        monkeypatch.setattr(session, "commit", original_commit)
        assert store.redis.values
    with sessions() as session:
        assert session.get(Operation, request["operation_id"]).status == "uncertain"
        assert session.get(CommercialIntent, request["instance_id"]).bootstrap_status == "uncertain"


def test_audit_actor_is_bounded_for_maximum_actor_and_reference_lengths(
    sessions, uncertain_bootstrap
):
    request, store = uncertain_bootstrap
    request["actor"] = "A" * 128
    request["change_reference"] = "R" * 256
    result = run_recovery(sessions, request, store)
    assert result["changed"] is True
    assert len(result["event_actor"]) <= 128
    assert recovery.build_audit_actor(
        request["actor"], request["change_reference"],
        request["evidence"]["evidence_sha256"],
    ) == result["event_actor"]


def test_missing_redis_key_is_idempotent_and_clears_persisted_reference(
    sessions, uncertain_bootstrap
):
    request, store = uncertain_bootstrap
    store.redis.values.clear()
    result = run_recovery(sessions, request, store)
    assert result["cleanup_pending"] is False
    with sessions() as session:
        assert session.get(CommercialIntent, request["instance_id"]).secret_reference is None


class RedisUnavailable:
    def delete(self, _reference):
        raise BootstrapSecretError("BOOTSTRAP_SECRET_CLEANUP_PENDING")


def test_redis_unavailable_leaves_durable_cleanup_marker_for_retry(
    sessions, uncertain_bootstrap
):
    request, store = uncertain_bootstrap
    result = run_recovery(sessions, request, RedisUnavailable())
    assert result["changed"] is True
    assert result["cleanup_pending"] is True
    with sessions() as session:
        assert session.get(Operation, request["operation_id"]).status == "failed"
        assert session.get(CommercialIntent, request["instance_id"]).secret_reference is not None

    retry = run_recovery(sessions, request, store)
    assert retry["already_completed"] is True
    assert retry["cleanup_pending"] is False
    with sessions() as session:
        intent = session.get(CommercialIntent, request["instance_id"])
        assert intent.secret_reference is None
        assert len(session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id == request["operation_id"],
            OperationEvent.code == recovery.EVENT_CODE,
        )).all()) == 1


def test_post_commit_reference_clear_failure_is_retryable(
    sessions, uncertain_bootstrap, monkeypatch
):
    request, store = uncertain_bootstrap
    with sessions() as session:
        original_commit = session.commit
        calls = {"count": 0}

        def fail_second_commit():
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("injected post-commit cleanup failure")
            return original_commit()

        monkeypatch.setattr(session, "commit", fail_second_commit)
        result = recovery.recover_in_session(
            session, request, runtime_idle=lambda: {"redis": True}, store=store
        )
        assert result["cleanup_pending"] is True
    with sessions() as session:
        assert session.get(Operation, request["operation_id"]).status == "failed"
        assert session.get(CommercialIntent, request["instance_id"]).secret_reference is not None

    retry = run_recovery(sessions, request, store)
    assert retry["already_completed"] is True
    assert retry["cleanup_pending"] is False


def test_ownership_mismatch_is_rejected(sessions, uncertain_bootstrap):
    request, store = uncertain_bootstrap
    with sessions() as session:
        session.get(TechnicalOwnership, request["instance_id"]).vmid = 30092
        session.commit()
    with pytest.raises(recovery.RecoveryError, match="TECHNICAL_OWNERSHIP_BINDING_CHANGED"):
        run_recovery(sessions, request, store)
    # La mutación no debe modificar una vinculación técnica histórica.
