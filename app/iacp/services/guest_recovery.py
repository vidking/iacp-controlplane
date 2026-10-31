"""Recuperación segura de Guest Initialization queued o consumida.

La recuperación no ejecuta comandos en el guest, Terraform ni Proxmox. En modo
``verify-consumed`` solo observa QGA y SSH con el fence ya consumido; nunca
reenvía guest-exec. La misma función es utilizada por la ruta administrativa
Core y por el driver de mantenimiento que puede inyectarla en un API anterior.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from iacp.models import (
    DeletionExecution,
    GuestInitialization,
    Instance,
    Job,
    Operation,
    OperationEvent,
    PowerExecution,
    TechnicalOwnership,
    TerraformApply,
    TerraformExecution,
)
from iacp.services.guest_identity import TERMINAL_GUEST_RECONCILIATION_FAILURE
from iacp.services.instances import Conflict, event
from iacp.services.technical_ownership import is_managed_vmid
from iacp.terraform.locks import admission_lock, instance_lock


ERROR_CODE = "GUEST_INITIALIZATION_CANCELLED_BEFORE_DISPATCH"
EVENT_CODE = "guest_initialization_cancelled_before_dispatch"
VERIFY_CONSUMED_MODE = "verify-consumed"
VERIFY_CONSUMED_EVENT = "guest_initialization_reconciled"
VERIFY_CONSUMED_FAILURE_EVENT = "guest_initialization_reconciliation_failed"
VERIFY_CONSUMED_FAILURE = TERMINAL_GUEST_RECONCILIATION_FAILURE
ACTIVE_OPERATION_STATUSES = ("queued", "running", "uncertain")
ACTIVE_APPLY_STATUSES = ("approved", "queued", "running", "uncertain")
PROTECTED_VMIDS = frozenset({104, 29000, 29001, 29010, 29011})
CONSUMED_UNCERTAIN_ERRORS = frozenset({
    # El worker puede agotar su ventana de observación después de consumir el
    # fence. verify-consumed puede cerrar únicamente con evidencia read-only;
    # nunca convierte este código en permiso para publicar otro guest-exec.
    "GUEST_INITIALIZATION_OBSERVATION_TIMEOUT",
    "ROOT_ACCESS_CONFIGURATION_MISMATCH",
    "ROOT_ACCESS_EVIDENCE_INVALID",
    "ROOT_ACCESS_VERIFICATION_UNCONFIRMED",
    "ROOT_SSH_AUTHENTICATION_FAILED",
    "ROOT_SSH_HOST_KEY_MISMATCH",
    # Versiones anteriores perdían el tipo Paramiko y persistían este código
    # genérico. verify-consumed lo acepta solo para reclasificarlo mediante
    # observación read-only; nunca autoriza un replay de guest-exec.
    "GUEST_INITIALIZATION_OBSERVATION_FAILED",
})


class RecoveryError(RuntimeError):
    """Fallo cerrado de una recuperación administrativa acotada."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise RecoveryError(reason)


def _utcnow():
    return datetime.now(timezone.utc)


def _canonical_uuid(value: str, field: str) -> str:
    try:
        canonical = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RecoveryError(f"{field.upper()}_INVALID") from exc
    _require(canonical == value, f"{field.upper()}_NON_CANONICAL")
    return canonical


def validate_request(request: dict) -> dict:
    """Valida los identificadores antes de abrir locks o consultar estado."""
    _require(isinstance(request, dict), "RECOVERY_REQUEST_INVALID")
    instance_id = _canonical_uuid(request.get("instance_id"), "instance_id")
    operation_id = _canonical_uuid(request.get("operation_id"), "operation_id")
    job_id = _canonical_uuid(request.get("job_id"), "job_id")
    try:
        vmid = int(request.get("vmid"))
    except (TypeError, ValueError) as exc:
        raise RecoveryError("VMID_INVALID") from exc
    _require(is_managed_vmid(vmid) and vmid not in PROTECTED_VMIDS,
             "VMID_NOT_RECOVERABLE")
    actor = request.get("actor")
    reference = request.get("change_reference")
    mode = request.get("mode", "cancel-queued")
    _require(mode in {"cancel-queued", VERIFY_CONSUMED_MODE}, "RECOVERY_MODE_INVALID")
    _require(isinstance(actor, str) and 1 <= len(actor) <= 128,
             "OPERATOR_IDENTITY_INVALID")
    _require(isinstance(reference, str) and 1 <= len(reference) <= 256,
             "CHANGE_REFERENCE_INVALID")
    return {
        "instance_id": instance_id,
        "operation_id": operation_id,
        "job_id": job_id,
        "vmid": vmid,
        "actor": actor,
        "change_reference": reference,
        "mode": mode,
    }


def assert_broker_and_workers_idle():
    """Falla cerrado si Redis o cualquier worker conserva trabajo ejecutable."""
    from redis import Redis
    from redis.exceptions import RedisError

    from iacp.config import broker_url
    from iacp.worker import celery_app

    try:
        with Redis.from_url(broker_url(), socket_connect_timeout=3, socket_timeout=3) as redis:
            for key in redis.scan_iter(count=100):
                name = key.decode("utf8", errors="replace")
                if name.split("\x06\x16")[0] in {"celery", "terraform", "lifecycle"}:
                    _require(redis.llen(key) == 0, "BROKER_PENDING_WORK")
            _require(redis.hlen("unacked") == 0, "BROKER_UNACKED_WORK")
    except RedisError:
        raise RecoveryError("BROKER_UNAVAILABLE") from None

    inspector = celery_app.control.inspect(timeout=5)
    reports = {}
    for method in ("active", "reserved", "scheduled"):
        try:
            result = getattr(inspector, method)()
        except Exception:
            raise RecoveryError(f"WORKER_{method.upper()}_UNAVAILABLE") from None
        _require(result is not None, f"WORKER_{method.upper()}_UNAVAILABLE")
        reports[method] = result
        _require(all(not tasks for tasks in result.values()),
                 f"WORKER_{method.upper()}_BUSY")
    return {"redis": True, "workers": {key: len(value) for key, value in reports.items()}}


def _load_locked_state(session, request: dict):
    instance = session.get(Instance, request["instance_id"], with_for_update=True,
                           populate_existing=True)
    _require(instance is not None, "INSTANCE_NOT_FOUND")
    _require(instance.vmid == request["vmid"], "INSTANCE_VMID_MISMATCH")
    _require(instance.status == "ready", "INSTANCE_NOT_READY")

    initialization = session.get(GuestInitialization, request["operation_id"],
                                 with_for_update=True, populate_existing=True)
    _require(initialization is not None, "GUEST_INITIALIZATION_NOT_FOUND")
    _require(initialization.instance_id == instance.id,
             "GUEST_INITIALIZATION_INSTANCE_MISMATCH")

    operation = session.get(Operation, request["operation_id"], with_for_update=True,
                            populate_existing=True)
    _require(operation is not None, "OPERATION_NOT_FOUND")
    _require(operation.instance_id == instance.id
             and operation.tenant_id == instance.tenant_id,
             "OPERATION_BINDING_CHANGED")
    _require(operation.kind == "instance.network.initialize", "OPERATION_KIND_MISMATCH")

    job = session.scalar(select(Job).where(
        Job.id == request["job_id"], Job.operation_id == operation.id,
    ).with_for_update())
    _require(job is not None, "JOB_NOT_FOUND")
    _require(job.tenant_id == instance.tenant_id, "JOB_BINDING_CHANGED")
    _require(job.operation == operation.kind, "JOB_KIND_MISMATCH")

    ownership = session.get(TechnicalOwnership, instance.id, with_for_update=True,
                            populate_existing=True)
    execution = session.get(TerraformExecution, instance.id, with_for_update=True,
                            populate_existing=True)
    _require(ownership is not None and execution is not None,
             "TECHNICAL_OWNERSHIP_REQUIRED")
    _require(
        ownership.tenant_id == instance.tenant_id
        and ownership.vmid == request["vmid"]
        and ownership.node == instance.node
        and ownership.name == instance.name
        and execution.vmid == request["vmid"]
        and ownership.state_key == execution.state_key,
        "TECHNICAL_OWNERSHIP_BINDING_CHANGED",
    )

    consumed = session.scalar(select(OperationEvent.id).where(
        OperationEvent.operation_id == operation.id,
        OperationEvent.code == "guest_initialization_dispatch_consumed",
    ).limit(1))

    other_operations = session.scalars(select(Operation).where(
        Operation.instance_id == instance.id,
        Operation.status.in_(ACTIVE_OPERATION_STATUSES),
        Operation.id != operation.id,
    ).with_for_update()).all()
    active_power = session.scalar(select(PowerExecution.operation_id).where(
        PowerExecution.instance_id == instance.id,
        PowerExecution.status.in_(ACTIVE_OPERATION_STATUSES),
    ).limit(1))
    active_delete = session.scalar(select(DeletionExecution.operation_id).where(
        DeletionExecution.instance_id == instance.id,
        DeletionExecution.status.in_(ACTIVE_OPERATION_STATUSES),
    ).limit(1))
    active_apply = session.scalar(select(TerraformApply.id).where(
        TerraformApply.instance_id == instance.id,
        TerraformApply.status.in_(ACTIVE_APPLY_STATUSES),
    ).limit(1))
    reconciled = session.scalar(select(OperationEvent.id).where(
        OperationEvent.operation_id == operation.id,
        OperationEvent.code.in_((VERIFY_CONSUMED_EVENT, VERIFY_CONSUMED_FAILURE_EVENT)),
    ).limit(1))

    return {
        "instance": instance,
        "initialization": initialization,
        "operation": operation,
        "job": job,
        "ownership": ownership,
        "execution": execution,
        "consumed_event_id": consumed,
        "other_operations": other_operations,
        "active_power": active_power,
        "active_delete": active_delete,
        "active_apply": active_apply,
        "reconciled_event_id": reconciled,
        "reconciled_success_event_id": session.scalar(select(OperationEvent.id).where(
            OperationEvent.operation_id == operation.id,
            OperationEvent.code == VERIFY_CONSUMED_EVENT,
        ).limit(1)),
        "reconciled_failure_event_id": session.scalar(select(OperationEvent.id).where(
            OperationEvent.operation_id == operation.id,
            OperationEvent.code == VERIFY_CONSUMED_FAILURE_EVENT,
        ).limit(1)),
    }


def _already_cancelled(state: dict) -> bool:
    initialization = state["initialization"]
    operation = state["operation"]
    job = state["job"]
    return (
        initialization.status == "failed"
        and operation.status == "failed"
        and operation.error_code == ERROR_CODE
        and job.status == "failed"
        and state["consumed_event_id"] is None
    )


def _validate_queued_state(state: dict) -> None:
    initialization = state["initialization"]
    operation = state["operation"]
    job = state["job"]
    _require(initialization.status == "queued", "GUEST_INITIALIZATION_NOT_QUEUED")
    _require(operation.status == "queued", "OPERATION_NOT_QUEUED")
    _require(operation.error_code is None, "OPERATION_HAS_ERROR")
    _require(job.status == "queued", "JOB_NOT_QUEUED")
    _require(initialization.started_at is None, "GUEST_INITIALIZATION_STARTED")
    _require(initialization.pid is None, "GUEST_INITIALIZATION_PID_PRESENT")
    _require(initialization.finished_at is None, "GUEST_INITIALIZATION_FINISHED")
    _require(initialization.result is None, "GUEST_INITIALIZATION_RESULT_PRESENT")
    _require(state["consumed_event_id"] is None,
             "GUEST_INITIALIZATION_DISPATCH_CONSUMED")
    _require(not state["other_operations"], "INSTANCE_OTHER_OPERATION_ACTIVE")
    _require(state["active_power"] is None, "INSTANCE_POWER_OPERATION_ACTIVE")
    _require(state["active_delete"] is None, "INSTANCE_DELETE_OPERATION_ACTIVE")
    _require(state["active_apply"] is None, "INSTANCE_APPLY_OPERATION_ACTIVE")


def _validate_consumed_state(state: dict) -> None:
    """Valida la forma exacta de una ejecución técnica consumida y dudosa."""
    initialization = state["initialization"]
    operation = state["operation"]
    job = state["job"]
    _require(initialization.status == "uncertain", "GUEST_INITIALIZATION_NOT_UNCERTAIN")
    _require(operation.status == "uncertain", "OPERATION_NOT_UNCERTAIN")
    _require(job.status == "failed", "GUEST_INITIALIZATION_JOB_ACTIVE")
    _require(initialization.started_at is not None, "GUEST_INITIALIZATION_NOT_CONSUMED")
    _require(initialization.pid is not None, "GUEST_INITIALIZATION_PID_MISSING")
    _require(initialization.finished_at is None, "GUEST_INITIALIZATION_ALREADY_TERMINAL")
    _require(initialization.result is None, "GUEST_INITIALIZATION_RESULT_PRESENT")
    _require(state["consumed_event_id"] is not None,
             "GUEST_INITIALIZATION_DISPATCH_NOT_CONSUMED")
    _require(operation.error_code in CONSUMED_UNCERTAIN_ERRORS,
             "GUEST_INITIALIZATION_ERROR_NOT_RECONCILIABLE")
    _require(not state["other_operations"], "INSTANCE_OTHER_OPERATION_ACTIVE")
    _require(state["active_power"] is None, "INSTANCE_POWER_OPERATION_ACTIVE")
    _require(state["active_delete"] is None, "INSTANCE_DELETE_OPERATION_ACTIVE")
    _require(state["active_apply"] is None, "INSTANCE_APPLY_OPERATION_ACTIVE")

    from .contracts import instance_uses_technical_access
    from .guest_identity import technical_program_for_mode

    instance = state["instance"]
    _require(instance_uses_technical_access(instance),
             "GUEST_INITIALIZATION_TECHNICAL_ACCESS_REQUIRED")
    try:
        expected = technical_program_for_mode(initialization.mode)[1]
    except Exception:
        raise RecoveryError("GUEST_INITIALIZATION_PROGRAM_NOT_AUTHORIZED") from None
    _require(initialization.program_sha256 == expected,
             "GUEST_INITIALIZATION_PROGRAM_CHANGED")
    _require(instance.technical_access_reference is not None,
             "ROOT_ACCESS_SECRET_UNAVAILABLE")


def _safe_reconciliation_failure(failure):
    """Clasifica solo fallos deterministas; conserva incertidumbre ante transporte."""
    from ..lifecycle.proxmox import PowerError
    from .access import SAFE_ACCESS_ERROR_CODES, access_error_code
    from .bootstrap_secrets import BootstrapSecretError
    from .guest_identity import GuestProgramFailed
    import paramiko

    if isinstance(failure, GuestProgramFailed):
        return "GUEST_INITIALIZATION_PROGRAM_FAILED"
    if isinstance(failure, BootstrapSecretError):
        code = str(failure)
        if code == "BOOTSTRAP_SECRET_EXPIRED_OR_MISSING":
            return VERIFY_CONSUMED_FAILURE
        raise RecoveryError(code) from None
    if isinstance(failure, PowerError):
        raise RecoveryError(str(failure)) from None
    if isinstance(failure, (paramiko.AuthenticationException, paramiko.BadHostKeyException)):
        return access_error_code(failure)
    if isinstance(failure, Conflict) and str(failure) in SAFE_ACCESS_ERROR_CODES:
        return str(failure)
    raise RecoveryError("GUEST_INITIALIZATION_RECONCILIATION_UNCONFIRMED") from None


def _already_reconciled(state: dict) -> bool:
    initialization = state["initialization"]
    operation = state["operation"]
    job = state["job"]
    return (
        initialization.status == "completed"
        and operation.status == "completed"
        and job.status == "completed"
        and operation.error_code is None
        and state["reconciled_success_event_id"] is not None
    )


def _already_reconciliation_failed(state: dict) -> bool:
    initialization = state["initialization"]
    operation = state["operation"]
    job = state["job"]
    return (
        initialization.status == "failed"
        and operation.status == "failed"
        and job.status == "failed"
        and operation.error_code in CONSUMED_UNCERTAIN_ERRORS | {VERIFY_CONSUMED_FAILURE}
        and state["reconciled_failure_event_id"] is not None
    )


def recover_in_session(session, request: dict, *, runtime_idle=None) -> dict:
    """Cancela exactamente una inicialización no despachada de forma atómica."""
    request = validate_request(request)
    if runtime_idle is None:
        runtime_idle = assert_broker_and_workers_idle
    try:
        with admission_lock(session), instance_lock(session.get_bind(), request["instance_id"]):
            state = _load_locked_state(session, request)
            if _already_cancelled(state):
                existing = session.scalar(select(OperationEvent).where(
                    OperationEvent.operation_id == request["operation_id"],
                    OperationEvent.code == EVENT_CODE,
                ).order_by(OperationEvent.id.desc()).limit(1))
                _require(existing is not None, "CANCELLATION_AUDIT_MISSING")
                return {
                    "changed": False,
                    "already_completed": True,
                    "instance_id": request["instance_id"],
                    "operation_id": request["operation_id"],
                    "job_id": request["job_id"],
                    "status": "failed",
                    "error_code": ERROR_CODE,
                    "event_code": EVENT_CODE,
                    "mode": "cancel-queued",
                    "event_id": existing.id,
                }

            _validate_queued_state(state)
            runtime_evidence = runtime_idle()
            session.expire_all()
            state = _load_locked_state(session, request)
            _validate_queued_state(state)

            initialization = state["initialization"]
            operation = state["operation"]
            job = state["job"]
            now = _utcnow()
            initialization.status = "failed"
            initialization.finished_at = now
            operation.status = "failed"
            operation.error_code = ERROR_CODE
            job.status = "failed"
            audit_actor = f"recover-queued-guest-init:{request['actor']}:{request['change_reference']}"
            _require(len(audit_actor) <= 128, "AUDIT_ACTOR_TOO_LONG")
            event(session, operation, EVENT_CODE, audit_actor)
            session.commit()
            return {
                "changed": True,
                "already_completed": False,
                "instance_id": request["instance_id"],
                "operation_id": request["operation_id"],
                "job_id": request["job_id"],
                "vmid": request["vmid"],
                "error_code": ERROR_CODE,
                "event_code": EVENT_CODE,
                "event_actor": audit_actor,
                "finished_at": now.isoformat(),
                "runtime": runtime_evidence,
            }
    except Exception:
        session.rollback()
        raise


def reconcile_consumed_in_session(session, request: dict, *, runtime_idle=None,
                                  guest_factory=None, verifier=None) -> dict:
    """Reconcilia una inicialización técnica consumida mediante observación.

    No ejecuta guest-exec, no reabre el fence y no modifica Terraform ni
    Proxmox. Solo puede cerrar como completada una ejecución ``uncertain`` cuya
    evidencia de guest, State, ownership, locks y acceso root sea verificable.
    Un fallo determinista se conserva como fallo terminal auditable para que el
    DELETE canónico pueda actuar; los fallos de transporte dejan la operación
    en ``uncertain``.
    """
    request = validate_request(request)
    _require(request["mode"] == VERIFY_CONSUMED_MODE, "RECOVERY_MODE_INVALID")
    if runtime_idle is None:
        runtime_idle = assert_broker_and_workers_idle

    cleanup_reference = None
    result = None
    try:
        with admission_lock(session), instance_lock(session.get_bind(), request["instance_id"]):
            state = _load_locked_state(session, request)
            if _already_reconciled(state):
                return {
                    "changed": False, "already_completed": True,
                    "instance_id": request["instance_id"],
                    "operation_id": request["operation_id"], "job_id": request["job_id"],
                    "status": "completed", "error_code": None,
                    "event_code": VERIFY_CONSUMED_EVENT, "mode": VERIFY_CONSUMED_MODE,
                }
            if _already_reconciliation_failed(state):
                return {
                    "changed": False, "already_completed": True,
                    "instance_id": request["instance_id"],
                    "operation_id": request["operation_id"], "job_id": request["job_id"],
                    "status": "failed", "error_code": state["operation"].error_code,
                    "event_code": VERIFY_CONSUMED_FAILURE_EVENT, "mode": VERIFY_CONSUMED_MODE,
                }

            _validate_consumed_state(state)
            runtime_evidence = runtime_idle()
            session.expire_all()
            state = _load_locked_state(session, request)
            _validate_consumed_state(state)

            instance = state["instance"]
            initialization = state["initialization"]
            operation = state["operation"]
            job = state["job"]
            from ..lifecycle.service import managed_target
            from .guest_identity import GuestClient

            target, _ = managed_target(session, instance)
            guest = guest_factory() if guest_factory is not None else GuestClient.configured()
            observed = None
            try:
                observed = guest.observe(target, initialization)
                if observed is None:
                    raise RecoveryError("GUEST_INITIALIZATION_EXEC_ACTIVE")
                network = observed.get("network") if isinstance(observed, dict) else None
                addresses = network.get("ip_addresses") if isinstance(network, dict) else None
                if not isinstance(addresses, list) or not addresses:
                    raise Conflict("ROOT_ACCESS_EVIDENCE_INVALID")

                from .access import AccessVerifier
                from .bootstrap_secrets import EphemeralSecrets
                password = EphemeralSecrets.configured().read(
                    instance.technical_access_reference,
                    instance.tenant_id,
                    instance.id,
                )
                access_result = (verifier or AccessVerifier()).verify(
                    target, instance, password=password, ip_addresses=addresses)
                if (access_result.get("cloud_init_complete") is not True
                        or access_result.get("ssh_authenticated") is not True):
                    raise Conflict("ROOT_ACCESS_EVIDENCE_INVALID")
            except RecoveryError:
                raise
            except Exception as failure:
                code = _safe_reconciliation_failure(failure)
                now = _utcnow()
                initialization.status = operation.status = job.status = "failed"
                initialization.finished_at = now
                operation.error_code = code
                instance.access_verification = {"status": "failed", "error_code": code}
                if observed is not None:
                    initialization.result = {
                        **observed,
                        "reconciliation": {"status": "failed", "error_code": code},
                    }
                    network = observed.get("network") if isinstance(observed, dict) else None
                    if isinstance(network, dict):
                        instance.ip_addresses = list(network.get("ip_addresses") or [])
                        instance.network_observation = {
                            **network,
                            "observed_at": now.isoformat(),
                            "identity_initialization": "reconciliation_failed",
                            "initialization_operation_id": initialization.operation_id,
                        }
                actor = f"reconcile-consumed-guest-init:{request['actor']}"
                _require(len(actor) <= 128, "AUDIT_ACTOR_TOO_LONG")
                event(session, operation, VERIFY_CONSUMED_FAILURE_EVENT, actor)
                session.commit()
                return {
                    "changed": True, "already_completed": False,
                    "instance_id": request["instance_id"],
                    "operation_id": request["operation_id"], "job_id": request["job_id"],
                    "status": "failed", "error_code": code,
                    "event_code": VERIFY_CONSUMED_FAILURE_EVENT,
                    "mode": VERIFY_CONSUMED_MODE, "runtime": runtime_evidence,
                }

            now = _utcnow()
            initialization.result = {
                **observed,
                "technical_access": {
                    "password_configured": True,
                    "ssh_authenticated": True,
                },
            }
            initialization.status = operation.status = job.status = "completed"
            initialization.finished_at = now
            operation.error_code = None
            instance.access_verification = {
                "status": "completed", "cloud_init_complete": True,
                "ssh_authenticated": True, "observed_at": now.isoformat(),
            }
            network = observed["network"]
            instance.ip_addresses = list(network["ip_addresses"])
            instance.network_observation = {
                **network,
                "observed_at": now.isoformat(),
                "identity_initialization": "reconciled",
                "initialization_operation_id": initialization.operation_id,
            }
            actor = f"reconcile-consumed-guest-init:{request['actor']}"
            _require(len(actor) <= 128, "AUDIT_ACTOR_TOO_LONG")
            event(session, operation, VERIFY_CONSUMED_EVENT, actor)
            session.commit()
            cleanup_reference = instance.technical_access_reference
            result = {
                "changed": True, "already_completed": False,
                "instance_id": request["instance_id"],
                "operation_id": request["operation_id"], "job_id": request["job_id"],
                "status": "completed", "error_code": None,
                "event_code": VERIFY_CONSUMED_EVENT,
                "mode": VERIFY_CONSUMED_MODE, "runtime": runtime_evidence,
            }
    except Exception:
        session.rollback()
        raise

    if cleanup_reference:
        # La evidencia durable ya está confirmada; si el almacén efímero está
        # temporalmente indisponible, la siguiente entrega solo reintenta la
        # limpieza y no vuelve a ejecutar guest-exec.
        try:
            from .guest_identity import _cleanup_technical_access_reference
            from ..db import get_session_factory
            _cleanup_technical_access_reference(
                request["instance_id"], cleanup_reference, get_session_factory())
        except Exception:
            pass
    return result


def recover_queued_initialization(session, instance, principal, operation_id: str,
                                  job_id: str, *, runtime_idle=None) -> dict:
    """API administrativa: deriva tenant, VMID y actor del contexto autenticado."""
    if principal.tenant_id != instance.tenant_id:
        raise Conflict("GUEST_INITIALIZATION_BINDING_CHANGED")
    actor = "api:" + principal.subject[:124]
    request = {
        "instance_id": instance.id,
        "operation_id": operation_id,
        "job_id": job_id,
        "vmid": instance.vmid,
        "actor": actor,
        "change_reference": "api:" + instance.id + ":" + operation_id,
        "mode": "cancel-queued",
    }
    if runtime_idle is None:
        runtime_idle = assert_broker_and_workers_idle
    try:
        result = recover_in_session(session, request, runtime_idle=runtime_idle)
        result.setdefault("status", "failed")
        result.setdefault("mode", "cancel-queued")
        return result
    except RecoveryError as failure:
        raise Conflict(str(failure)) from None


def reconcile_consumed_initialization(session, instance, principal, operation_id: str,
                                      job_id: str, *, runtime_idle=None,
                                      guest_factory=None, verifier=None) -> dict:
    """API administrativa para verificar una ejecución consumida sin replay."""
    if principal.tenant_id != instance.tenant_id:
        raise Conflict("GUEST_INITIALIZATION_BINDING_CHANGED")
    request = {
        "instance_id": instance.id,
        "operation_id": operation_id,
        "job_id": job_id,
        "vmid": instance.vmid,
        "actor": "api:" + principal.subject[:124],
        "change_reference": "api:" + instance.id + ":" + operation_id,
        "mode": VERIFY_CONSUMED_MODE,
    }
    if runtime_idle is None:
        runtime_idle = assert_broker_and_workers_idle
    try:
        return reconcile_consumed_in_session(
            session, request, runtime_idle=runtime_idle,
            guest_factory=guest_factory, verifier=verifier,
        )
    except RecoveryError as failure:
        raise Conflict(str(failure)) from None


def run_from_json(payload: str) -> None:
    """Punto de entrada del driver; solo imprime evidencia no sensible."""
    from iacp.db import get_session_factory

    try:
        request = json.loads(payload)
        with get_session_factory()() as session:
            result = recover_in_session(session, request)
        print(json.dumps({"passed": True, **result}, sort_keys=True, default=str))
    except RecoveryError as error:
        print(json.dumps({"passed": False, "reason": str(error)}, sort_keys=True))
    except Exception as error:
        print(json.dumps({"passed": False, "reason": type(error).__name__}, sort_keys=True))
