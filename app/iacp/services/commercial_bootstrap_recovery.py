"""Cierre administrativo de un bootstrap comercial incierto antes de SSH.

Este flujo no reintenta la entrega y no ejecuta QGA, SSH, Terraform ni
Proxmox. Solo puede cerrar una operación histórica cuando la evidencia
persistida identifica exactamente el runtime, el orden de preflight y el
fallo previo a ``ssh.connect()``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from iacp.models import (
    CommercialIntent,
    DeletionExecution,
    Instance,
    Job,
    Operation,
    OperationEvent,
    PowerExecution,
    TechnicalOwnership,
    TerraformApply,
    TerraformExecution,
)
from iacp.services.bootstrap_secrets import EphemeralSecrets
from iacp.services.instances import event
from iacp.services.technical_ownership import is_managed_vmid
from iacp.terraform.locks import admission_lock, instance_lock


ERROR_CODE = "BOOTSTRAP_FAILED_BEFORE_SSH_CONNECT"
EVENT_CODE = "commercial_bootstrap_failed_before_ssh_connect"
PRIOR_ERROR_CODE = "BOOTSTRAP_RECONCILIATION_REQUIRED"
HISTORICAL_RUNTIME_SHA = "8b7ec3bc80d6de57a64f40a3670812aef08a621c"
SECURE_BOOTSTRAP_SHA256 = "23c19284d4c1673328f7917f97be3d9ead78c5d11b59bd117ec88fc7ddb94b58"
COMMERCIAL_BOOTSTRAP_SHA256 = "689d8d5350fceded7cc8e6dbc02fb05bafcffe60c41bdd3235ee90c260691918"
HOST_KEY_PATH = "/etc/ssh/ssh_host_ed25519_key.pub"
MARKER_PATH = "/var/lib/iacp/commercial-bootstrap.json"
OPERATION_KIND = "instance.access.bootstrap"
ACTIVE_OPERATION_STATUSES = ("queued", "running", "uncertain")
ACTIVE_APPLY_STATUSES = ("approved", "queued", "running", "uncertain")
PROTECTED_VMIDS = frozenset({104, 106, 29000, 29001, 29010, 29011})
PRECONNECT_SEQUENCE = (
    "guest_observe",
    "cloud_init_observe",
    "host_key_public_read",
    "host_key_parse",
    "ssh_connect_not_reached",
)
FORBIDDEN_EVENTS = frozenset({
    "commercial_bootstrap_completed",
    "commercial_bootstrap_credentials_delivered",
    "commercial_bootstrap_ssh_connected",
})


class RecoveryError(RuntimeError):
    """Fallo cerrado de una recuperación histórica específica."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise RecoveryError(reason)


def _utcnow():
    return datetime.now(timezone.utc)


def _canonical_uuid(value, field: str) -> str:
    try:
        canonical = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RecoveryError(f"{field.upper()}_INVALID") from exc
    _require(canonical == value, f"{field.upper()}_NON_CANONICAL")
    return canonical


def _has_sensitive_key(value) -> bool:
    if isinstance(value, dict):
        if any(re.search(r"password|secret|private.?key|ciphertext|token", str(key), re.I)
               for key in value):
            return True
        return any(_has_sensitive_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_sensitive_key(item) for item in value)
    return False


def validate_evidence(evidence: dict, expected: dict) -> dict:
    """Valida una atestación de inspección read-only, sin guardar su cuerpo."""
    _require(isinstance(evidence, dict), "RECOVERY_EVIDENCE_INVALID")
    _require(not _has_sensitive_key(evidence), "RECOVERY_EVIDENCE_CONTAINS_SECRET")
    _require(evidence.get("evidence_version") == "iacp.bootstrap-preconnect.v1",
             "RECOVERY_EVIDENCE_VERSION_UNSUPPORTED")
    for field in ("instance_id", "operation_id", "job_id"):
        _require(evidence.get(field) == expected[field], f"RECOVERY_EVIDENCE_{field.upper()}_MISMATCH")
    _require(evidence.get("vmid") == expected["vmid"], "RECOVERY_EVIDENCE_VMID_MISMATCH")
    _require(evidence.get("historical_runtime_sha") == HISTORICAL_RUNTIME_SHA,
             "RECOVERY_HISTORICAL_RUNTIME_MISMATCH")
    _require(evidence.get("secure_bootstrap_source_sha256") == SECURE_BOOTSTRAP_SHA256,
             "RECOVERY_SECURE_BOOTSTRAP_CODE_MISMATCH")
    _require(evidence.get("commercial_bootstrap_source_sha256") == COMMERCIAL_BOOTSTRAP_SHA256,
             "RECOVERY_COMMERCIAL_BOOTSTRAP_CODE_MISMATCH")
    _require(evidence.get("inspection_mode") == "read-only",
             "RECOVERY_EVIDENCE_NOT_READ_ONLY")
    _require(evidence.get("source") == "persisted-runtime-evidence",
             "RECOVERY_EVIDENCE_SOURCE_UNTRUSTED")
    _require(evidence.get("physical_identity_verified") is True,
             "RECOVERY_PHYSICAL_IDENTITY_UNVERIFIED")
    _require(evidence.get("ssh_connect_attempted") is False,
             "RECOVERY_SSH_CONNECT_ALREADY_ATTEMPTED")
    _require(evidence.get("credential_delivery_started") is False,
             "RECOVERY_CREDENTIAL_DELIVERY_STARTED")
    _require(evidence.get("preconnect_sequence") == list(PRECONNECT_SEQUENCE),
             "RECOVERY_PRECONNECT_SEQUENCE_MISMATCH")

    qga = evidence.get("qga")
    _require(isinstance(qga, dict)
             and qga.get("path") == HOST_KEY_PATH
             and qga.get("error_class") == "permission_denied"
             and qga.get("error_code") == "BOOTSTRAP_HOST_KEY_CHANNEL_UNAVAILABLE",
             "RECOVERY_QGA_EVIDENCE_MISMATCH")
    marker = evidence.get("marker")
    _require(isinstance(marker, dict)
             and marker.get("path") == MARKER_PATH
             and marker.get("status") == "absent",
             "RECOVERY_MARKER_EVIDENCE_MISMATCH")
    reference = evidence.get("evidence_reference")
    _require(isinstance(reference, str) and 1 <= len(reference) <= 512,
             "RECOVERY_EVIDENCE_REFERENCE_INVALID")
    evidence_sha256 = evidence.get("evidence_sha256")
    _require(isinstance(evidence_sha256, str)
             and re.fullmatch(r"[a-f0-9]{64}", evidence_sha256),
             "RECOVERY_EVIDENCE_DIGEST_INVALID")
    return {
        "evidence_reference": reference,
        "evidence_sha256": evidence_sha256,
        "historical_runtime_sha": HISTORICAL_RUNTIME_SHA,
        "ssh_connect_attempted": False,
        "credential_delivery_started": False,
    }


def validate_request(request: dict) -> dict:
    _require(isinstance(request, dict), "RECOVERY_REQUEST_INVALID")
    values = {field: _canonical_uuid(request.get(field), field)
              for field in ("instance_id", "operation_id", "job_id")}
    try:
        vmid = int(request.get("vmid"))
    except (TypeError, ValueError) as exc:
        raise RecoveryError("VMID_INVALID") from exc
    _require(is_managed_vmid(vmid) and vmid not in PROTECTED_VMIDS,
             "VMID_NOT_RECOVERABLE")
    actor = request.get("actor")
    reference = request.get("change_reference")
    _require(isinstance(actor, str) and 1 <= len(actor) <= 128,
             "OPERATOR_IDENTITY_INVALID")
    _require(isinstance(reference, str) and 1 <= len(reference) <= 256,
             "CHANGE_REFERENCE_INVALID")
    values.update({"vmid": vmid, "actor": actor, "change_reference": reference})
    values["evidence"] = validate_evidence(request.get("evidence"), values)
    return values


def assert_broker_and_workers_idle():
    """No permite cerrar el fence mientras exista trabajo ejecutable."""
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


def _load_locked_state(session, request: dict) -> dict:
    instance = session.get(Instance, request["instance_id"], with_for_update=True,
                           populate_existing=True)
    _require(instance is not None, "INSTANCE_NOT_FOUND")
    _require(instance.status == "ready", "INSTANCE_NOT_READY")
    _require(instance.vmid == request["vmid"], "INSTANCE_VMID_MISMATCH")

    intent = session.get(CommercialIntent, instance.id, with_for_update=True,
                         populate_existing=True)
    operation = session.get(Operation, request["operation_id"], with_for_update=True,
                            populate_existing=True)
    _require(intent is not None, "COMMERCIAL_INTENT_NOT_FOUND")
    _require(operation is not None, "OPERATION_NOT_FOUND")
    _require(operation.instance_id == instance.id
             and operation.tenant_id == instance.tenant_id
             and operation.kind == OPERATION_KIND,
             "OPERATION_BINDING_CHANGED")
    job = session.scalar(select(Job).where(
        Job.id == request["job_id"], Job.operation_id == operation.id,
    ).with_for_update())
    _require(job is not None and job.tenant_id == instance.tenant_id
             and job.operation == OPERATION_KIND, "JOB_BINDING_CHANGED")

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
        and execution.state_key == ownership.state_key
        and execution.state_key,
        "TECHNICAL_OWNERSHIP_BINDING_CHANGED",
    )
    apply = session.scalar(select(TerraformApply).where(
        TerraformApply.instance_id == instance.id,
        TerraformApply.attempt == execution.attempt,
        TerraformApply.plan_sha256 == execution.plan_sha256,
        TerraformApply.status == "completed",
    ).order_by(TerraformApply.id.desc()).limit(1))
    _require(apply is not None, "TERRAFORM_APPLY_EVIDENCE_REQUIRED")

    codes = set(session.scalars(select(OperationEvent.code).where(
        OperationEvent.operation_id == operation.id)).all())
    active_other = session.scalars(select(Operation).where(
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
    return {
        "instance": instance,
        "intent": intent,
        "operation": operation,
        "job": job,
        "ownership": ownership,
        "execution": execution,
        "apply": apply,
        "event_codes": codes,
        "active_other": active_other,
        "active_power": active_power,
        "active_delete": active_delete,
        "active_apply": active_apply,
    }


def _validate_uncertain_state(state: dict) -> None:
    intent, operation, job = state["intent"], state["operation"], state["job"]
    _require(intent.bootstrap_status == "uncertain", "BOOTSTRAP_NOT_UNCERTAIN")
    _require(operation.status == "uncertain", "OPERATION_NOT_UNCERTAIN")
    _require(job.status == "failed", "BOOTSTRAP_JOB_NOT_TERMINAL_FAILED")
    _require(intent.bootstrap_started_at is not None, "BOOTSTRAP_NOT_STARTED")
    _require(intent.bootstrap_completed_at is None, "BOOTSTRAP_ALREADY_COMPLETED")
    _require(intent.error_code == PRIOR_ERROR_CODE and operation.error_code == PRIOR_ERROR_CODE,
             "BOOTSTRAP_PRIOR_ERROR_MISMATCH")
    codes = state["event_codes"]
    _require("commercial_bootstrap_dispatch_consumed" in codes,
             "BOOTSTRAP_CONSUMPTION_EVIDENCE_MISSING")
    _require("commercial_bootstrap_uncertain" in codes,
             "BOOTSTRAP_UNCERTAIN_EVENT_MISSING")
    _require(not codes.intersection(FORBIDDEN_EVENTS), "BOOTSTRAP_COMPLETION_EVIDENCE_PRESENT")
    _require(not state["active_other"], "INSTANCE_OTHER_OPERATION_ACTIVE")
    _require(state["active_power"] is None, "INSTANCE_POWER_OPERATION_ACTIVE")
    _require(state["active_delete"] is None, "INSTANCE_DELETE_OPERATION_ACTIVE")
    _require(state["active_apply"] is None, "INSTANCE_APPLY_OPERATION_ACTIVE")


def _already_resolved(state: dict) -> bool:
    intent, operation, job = state["intent"], state["operation"], state["job"]
    return (
        intent.bootstrap_status == "failed"
        and operation.status == "failed"
        and operation.error_code == ERROR_CODE
        and job.status == "failed"
        and intent.bootstrap_completed_at is None
        and EVENT_CODE in state["event_codes"]
        and not state["event_codes"].intersection(FORBIDDEN_EVENTS)
        and intent.secret_reference is None
    )


def recover_in_session(session, request: dict, *, runtime_idle=None, store=None) -> dict:
    """Cierra solo el caso exacto y conserva toda la evidencia histórica."""
    request = validate_request(request)
    if runtime_idle is None:
        runtime_idle = assert_broker_and_workers_idle
    try:
        with admission_lock(session), instance_lock(session.get_bind(), request["instance_id"]):
            state = _load_locked_state(session, request)
            if _already_resolved(state):
                return {
                    "changed": False,
                    "already_completed": True,
                    "instance_id": request["instance_id"],
                    "operation_id": request["operation_id"],
                    "job_id": request["job_id"],
                    "error_code": ERROR_CODE,
                    "event_code": EVENT_CODE,
                    "evidence_sha256": request["evidence"]["evidence_sha256"],
                }

            _validate_uncertain_state(state)
            runtime_evidence = runtime_idle()
            session.expire_all()
            state = _load_locked_state(session, request)
            _validate_uncertain_state(state)

            intent, operation, job, instance = (
                state["intent"], state["operation"], state["job"], state["instance"])
            # Solo se elimina la referencia efímera opaca; nunca se descifra ni
            # se vuelve a entregar la contraseña. Si Redis falla, el rollback
            # deja intacto el fence y no se cambia ningún estado ORM.
            if intent.secret_reference:
                (store or EphemeralSecrets.configured()).delete(intent.secret_reference)
                intent.secret_reference = None

            now = _utcnow()
            intent.bootstrap_status = "failed"
            intent.error_code = ERROR_CODE
            operation.status = "failed"
            operation.error_code = ERROR_CODE
            job.status = "failed"
            instance.access_verification = {
                **(instance.access_verification or {}),
                "status": "failed",
                "error_code": ERROR_CODE,
            }
            audit_actor = (
                f"recover-commercial-bootstrap-preconnect:{request['actor']}"
                f":{request['change_reference']}:{request['evidence']['evidence_sha256'][:16]}"
            )
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
                "status": "failed",
                "error_code": ERROR_CODE,
                "event_code": EVENT_CODE,
                "event_actor": audit_actor,
                "finished_at": now.isoformat(),
                "evidence_reference": request["evidence"]["evidence_reference"],
                "evidence_sha256": request["evidence"]["evidence_sha256"],
                "runtime": runtime_evidence,
            }
    except Exception:
        session.rollback()
        raise


def run_from_json(payload: str) -> None:
    """Entrada del driver; nunca imprime cuerpos de evidencia ni secretos."""
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
