import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Callable

from fastapi import HTTPException

from api.catalog import (
    image_by_id,
    image_catalog as catalog_images,
    network_profile_by_id,
    plan_by_id,
    plan_catalog,
)
from api.db import database
from api.models import Action, Command, InstanceCreateRequest, InstanceSpec, OperationRequest, PowerAction
from api.protection import is_protected_vmid
from api.secrets import delete_secret, write_secret
from api.targets import EnvironmentError, resolve_environment


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(db, operation_id: str, kind: str, status: str, detail: str = "") -> None:
    db.execute(
        "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, ?, ?, ?, ?)",
        (operation_id, kind, status, now(), detail),
    )


ACTIVE_OPERATION_STATUSES = {
    "pending",
    "running",
    "planning",
    "planned",
    "applying",
    "destroying",
    "powering",
}

PUBLIC_OPERATION_STATUS = {
    "pending": "queued",
    "running": "running",
    "succeeded": "completed",
    "failed": "failed",
    "uncertain": "uncertain",
}

PUBLIC_INSTANCE_STATUS = {
    "pending": "pending",
    "planned": "pending",
    "planning": "provisioning",
    "applying": "provisioning",
    "running": "provisioning",
    "powering": "provisioning",
    "destroying": "deleting",
    "ready": "ready",
    "failed": "failed",
    "uncertain": "uncertain",
    "deleted": "deleted",
}


PUBLIC_EVENT_KIND_REWRITES = {
    "proxmox.pool.validated": "resource.pool.validated",
    "golden.observed": "image.validated",
    "logical.delete.completed": "instance.delete.completed",
}

PUBLIC_ERROR_CODES = {
    "POWER_PERMISSION_DENIED": "POWER_PERMISSION_DENIED",
    "POWER_TASK_UNCERTAIN": "POWER_OPERATION_UNCERTAIN",
    "POWER_OPERATION_UNCERTAIN": "POWER_OPERATION_UNCERTAIN",
    "POWER_TASK_FAILED": "POWER_OPERATION_FAILED",
    "POWER_PROXMOX_UNAVAILABLE": "POWER_SERVICE_UNAVAILABLE",
    "POWER_IDENTITY_MISMATCH": "POWER_IDENTITY_MISMATCH",
    "POWER_TASK_IDENTITY_MISMATCH": "POWER_IDENTITY_MISMATCH",
    "POWER_ACTION_INVALID": "POWER_ACTION_INVALID",
    "POWER_STATE_INVALID": "POWER_STATE_INVALID",
    "POWER_REBOOT_REQUIRES_REVIEW": "POWER_REBOOT_REQUIRES_REVIEW",
    "POWER_INSTANCE_NOT_PROVISIONED": "POWER_INSTANCE_NOT_PROVISIONED",
    "POWER_INSTANCE_DELETED": "POWER_INSTANCE_DELETED",
    "PROXMOX_POOL_REQUIRED": "RESOURCE_POOL_REQUIRED",
    "PROXMOX_POOL_NOT_FOUND": "RESOURCE_POOL_NOT_FOUND",
    "VMID_RANGE_EXHAUSTED": "VMID_ALLOCATION_EXHAUSTED",
    "SSH_PUBLIC_KEY_REQUIRED": "SSH_PUBLIC_KEY_REQUIRED",
    "RECONCILIATION_REQUIRED": "RECONCILIATION_REQUIRED",
    "RECONCILIATION_PHYSICAL_RESOURCE_ABSENT": "RECONCILIATION_PHYSICAL_RESOURCE_ABSENT",
    "PHYSICAL_IDENTITY_MISMATCH": "PHYSICAL_IDENTITY_MISMATCH",
    "PHYSICAL_NODE_MISMATCH": "PHYSICAL_NODE_MISMATCH",
}

INTERNAL_LOG_MARKERS = (
    "terraform",
    "fastapi",
    "uvicorn",
    "bpg/proxmox",
    "proxmox_virtual_environment",
    "provider plugin",
    "provider plugins",
    "hashicorp",
    "module.kvm",
    "plan.tfplan",
    "workspace_path",
    "plan_path",
    "/var/lib/iacp-controlplane",
    "/workspace/terraform",
    "initializing modules",
    "execution plan",
    "terraform has been successfully initialized",
    "authorization: bearer",
)


def public_instance_status(row) -> str:
    """Return the public lifecycle status, including observed power state.

    A provisioned instance is only ``ready`` while it is running.  The
    database keeps the provisioning status as ``ready`` so lifecycle actions
    continue to target the same managed instance, but a stopped VM is exposed
    separately to API consumers and the portal.
    """
    raw_status = row["status"]
    if raw_status == "ready" and row["power_state"] == "stopped":
        return "stopped"
    return PUBLIC_INSTANCE_STATUS.get(raw_status, raw_status)


def _active_instance_operation(db, instance_id: str):
    placeholders = ",".join("?" for _ in ACTIVE_OPERATION_STATUSES)
    return db.execute(
        f"""
        SELECT operation_id, command, action, status
        FROM operations
        WHERE instance_id = ? AND status IN ({placeholders})
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (instance_id, *sorted(ACTIVE_OPERATION_STATUSES)),
    ).fetchone()


def _latest_provision(db, instance_id: str):
    return db.execute(
        """
        SELECT * FROM operations
        WHERE instance_id = ? AND command IN ('provision', 'plan')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (instance_id,),
    ).fetchone()


def public_event_kind(kind: str | None) -> str | None:
    """Translate implementation events to the stable orchestration vocabulary."""
    if not kind:
        return kind
    if kind in PUBLIC_EVENT_KIND_REWRITES:
        return PUBLIC_EVENT_KIND_REWRITES[kind]
    if kind.startswith("terraform.plan."):
        return kind.replace("terraform.plan.", "provision.plan.", 1)
    if kind.startswith("terraform.provision.") or kind.startswith("terraform.apply."):
        suffix = kind.split(".", 2)[-1]
        return f"instance.create.{suffix}"
    if kind.startswith("terraform.destroy."):
        suffix = kind.split(".", 2)[-1]
        return f"instance.delete.{suffix}"
    if kind.startswith("recovery."):
        suffix = kind.split(".", 1)[-1]
        return f"instance.recovery.{suffix}"
    if "terraform" in kind.lower():
        return "instance.operation.failed" if kind.endswith(".failed") else "instance.operation"
    if "proxmox" in kind.lower():
        return "resource.operation"
    return kind


def public_error_code(error: str | None, status: str | None = None) -> str | None:
    if not error:
        return None
    raw = str(error)
    for private_code, public_code in PUBLIC_ERROR_CODES.items():
        if private_code in raw:
            return public_code
    lowered = raw.lower()
    if "reconciliacion fisica" in lowered or "physical reconciliation" in lowered:
        return "RECONCILIATION_FAILED"
    if status == "uncertain":
        return "OPERATION_UNCERTAIN"
    return "OPERATION_FAILED"


def _contains_internal_marker(value: object) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in INTERNAL_LOG_MARKERS)


def public_event_detail(kind: str | None, detail: str | None, status: str | None = None) -> str:
    if not detail:
        return ""
    raw = str(detail)
    if _contains_internal_marker(raw) or (kind and "terraform" in kind.lower()):
        code = public_error_code(raw, status) if status in {"failed", "uncertain"} else None
        return f"Código de operación: {code}" if code else ""
    if kind == "logical.delete.completed" or "no physical vm" in raw.lower():
        return "No había VM física; se eliminó el registro lógico."
    return raw[:512]


def public_operation_output(operation: dict) -> str:
    raw_output = str(operation.get("output") or "")
    if "no había vm física" in raw_output.lower():
        return "No había VM física; se eliminó el registro lógico fallido."
    public_status = PUBLIC_OPERATION_STATUS.get(operation.get("status"), operation.get("status"))
    command = operation.get("command")
    payload = operation.get("payload") or _public_payload(operation.get("payload_json"))
    action = payload.get("power_action") or operation.get("action")
    labels = {
        "provision": "aprovisionamiento",
        "plan": "validación de configuración",
        "apply": "aprovisionamiento",
        "destroy": "eliminación",
        "start": "encendido",
        "shutdown": "apagado",
        "reboot": "reinicio",
        "reconcile": "reconciliación",
    }
    label = labels.get(action, labels.get(command, "operación"))
    if public_status == "completed":
        message = f"La etapa de {label} terminó correctamente."
    elif public_status == "failed":
        message = f"La etapa de {label} no pudo completarse."
    elif public_status == "uncertain":
        message = f"La etapa de {label} requiere reconciliación física."
    elif public_status in {"queued", "running"}:
        message = f"La etapa de {label} está en ejecución."
    else:
        message = f"La etapa de {label} está registrada."
    if operation.get("vmid") is not None:
        message += f" VMID {operation['vmid']}."
    code = public_error_code(operation.get("error"), operation.get("status"))
    if public_status in {"failed", "uncertain"} and code:
        message += f" Código: {code}."
    return message


def public_operation_error(operation: dict) -> str:
    code = public_error_code(operation.get("error"), operation.get("status"))
    if not code:
        return ""
    return f"La operación no pudo confirmarse. Código: {code}."


def _public_payload(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    result = dict(value)
    for key in ("root_password", "secret_path", "workspace_path", "plan_path"):
        result.pop(key, None)
    return result


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        started = datetime.fromisoformat(start)
        finished = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return round(max(0.0, (finished - started).total_seconds()), 3)


def operation_timings(row: dict) -> dict:
    """Expose safe lifecycle timings without internal execution details."""

    created = row.get("created_at")
    started = row.get("started_at")
    finished = row.get("finished_at")
    return {
        "queue_seconds": _duration_seconds(created, started),
        "execution_seconds": _duration_seconds(started, finished),
        "total_seconds": _duration_seconds(created, finished),
    }


def event_row(db, row):
    if row is None:
        return None
    result = dict(row)
    result["kind"] = public_event_kind(result.get("kind"))
    result["detail"] = public_event_detail(row["kind"], result.get("detail"), result.get("status"))
    result.setdefault("command", None)
    result.setdefault("action", None)
    result.setdefault("instance_id", None)
    result.setdefault("instance_name", None)
    result.setdefault("worker_id", None)
    return result


def latest_event(db, operation_id: str | None):
    if not operation_id:
        return None
    row = db.execute(
        """
        SELECT e.*, o.command, o.action, o.instance_id, o.worker_id,
               i.name AS instance_name
        FROM events e
        LEFT JOIN operations o ON o.operation_id = e.operation_id
        LEFT JOIN instances i ON i.instance_id = o.instance_id
        WHERE e.operation_id = ?
        ORDER BY e.event_id DESC
        LIMIT 1
        """,
        (operation_id,),
    ).fetchone()
    return event_row(db, row)


def _request_plain_payload(request: InstanceCreateRequest) -> dict:
    access = {
        "ssh_public_keys": list(request.access.ssh_public_keys),
        "root_password": (
            request.access.root_password.get_secret_value()
            if request.access.root_password is not None
            else None
        ),
    }
    return {
        "name": request.name,
        "plan_id": request.plan_id,
        "image_id": request.image_id,
        "environment": request.environment,
        "network_profile": request.network_profile,
        "tags": request.tags,
        "access": access,
        "start_on_create": request.start_on_create,
    }


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_key_or_error(value: str | None) -> str:
    key = (value or "").strip()
    if not key:
        return f"generated-{uuid.uuid4()}"
    if len(key) > 255:
        raise HTTPException(status_code=422, detail="Idempotency-Key no puede superar 255 caracteres")
    return key


def _existing_idempotency(db, scope: str, key: str, request_hash: str):
    row = db.execute(
        "SELECT request_hash, response_json FROM idempotency_keys WHERE scope=? AND idempotency_key=?",
        (scope, key),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise HTTPException(status_code=409, detail="Idempotency-Key ya fue usada con otro cuerpo")
    return json.loads(row["response_json"])


def _idempotent_mutation(
    *,
    scope: str,
    idempotency_key: str | None,
    body: object,
    builder: Callable,
) -> dict:
    key = _idempotency_key_or_error(idempotency_key)
    request_hash = _fingerprint(body)
    with database() as db:
        db.execute("BEGIN")
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?), hashtext(?))", (scope, key))
        existing = _existing_idempotency(db, scope, key, request_hash)
        if existing is not None:
            db.execute("COMMIT")
            return existing
        db.execute(
            """
            INSERT INTO idempotency_keys(
              scope, idempotency_key, request_hash, response_json, status_code, operation_id, created_at
            ) VALUES (?, ?, ?, '{}', 202, NULL, ?)
            """,
            (scope, key, request_hash, now()),
        )
        try:
            response, operation_id = builder(db)
            response = dict(response)
            response.setdefault("idempotency_key", key)
            db.execute(
                """
                UPDATE idempotency_keys
                SET response_json=?, status_code=202, operation_id=?
                WHERE scope=? AND idempotency_key=?
                """,
                (json.dumps(response), operation_id, scope, key),
            )
            db.execute("COMMIT")
            return response
        except Exception:
            db.execute("ROLLBACK")
            raise


def _resolve_public_request(
    request: InstanceCreateRequest,
) -> tuple[InstanceSpec, dict, dict, str | None, str]:
    try:
        environment = resolve_environment(request.environment)
    except EnvironmentError as error:
        raise HTTPException(status_code=error.status_code, detail=error.code) from error
    plan = plan_by_id(request.plan_id, environment)
    if plan is None:
        raise HTTPException(status_code=422, detail=f"plan_id no disponible: {request.plan_id}")
    image = image_by_id(request.image_id, environment)
    if image is None:
        raise HTTPException(status_code=422, detail=f"image_id no disponible: {request.image_id}")
    if plan["vcpus"] < image.get("minimum_vcpus", 1):
        raise HTTPException(status_code=422, detail="el plan no cumple el mínimo de vCPU de la imagen")
    if plan["memory_mb"] < image.get("minimum_memory_mb", 0):
        raise HTTPException(status_code=422, detail="el plan no cumple el mínimo de memoria de la imagen")
    if plan["disk_gb"] < image.get("minimum_disk_gb", 0):
        raise HTTPException(status_code=422, detail="el plan no cumple el mínimo de disco de la imagen")
    profile = network_profile_by_id(request.network_profile)
    if profile is None:
        raise HTTPException(status_code=422, detail="network_profile no disponible")
    if not profile["enabled"]:
        raise HTTPException(status_code=503, detail="NETWORK_PROFILE_NOT_CONFIGURED")
    spec = InstanceSpec(
        name=request.name,
        image=request.image_id,
        vcpus=plan["vcpus"],
        memory_mb=plan["memory_mb"],
        disk_gb=plan["disk_gb"],
        network_profile=request.network_profile,
        network_mode="dhcp",
        ssh_public_keys=request.access.ssh_public_keys,
        started=request.start_on_create,
        tags=request.tags,
    )
    password = (
        request.access.root_password.get_secret_value()
        if request.access.root_password is not None
        else None
    )
    return spec, plan, image, password, environment


def _insert_operation(
    db,
    request: OperationRequest,
    *,
    secret_path: str | None = None,
    operation_id: str | None = None,
) -> tuple[dict, str]:
    operation_id = operation_id or str(uuid.uuid4())
    instance_id = str(uuid.uuid4()) if request.command in {Command.plan, Command.provision} else request.instance_id
    try:
        effective_environment = resolve_environment(request.environment)
    except EnvironmentError as error:
        raise HTTPException(status_code=error.status_code, detail=error.code) from error
    created = now()
    payload = request.model_dump(mode="json")

    if request.command in {Command.apply, Command.destroy, Command.power, Command.reconcile} and instance_id:
        protected = db.execute(
            "SELECT vmid FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if protected is not None and is_protected_vmid(protected["vmid"]):
            raise HTTPException(status_code=409, detail="PROTECTED_INSTANCE")

    if request.command in {Command.plan, Command.provision} and request.spec is not None:
        duplicate = db.execute(
            "SELECT instance_id FROM instances WHERE name = ? AND status != 'deleted' LIMIT 1",
            (request.spec.name,),
        ).fetchone()
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="ya existe una instancia con ese nombre")

    if request.command == Command.power:
        target = db.execute(
            """
            SELECT i.instance_id, i.status, i.environment
            FROM instances i
            WHERE i.instance_id = ?
            """,
            (instance_id,),
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="instance_id no existe")
        if target["status"] == "deleted":
            raise HTTPException(status_code=409, detail="la instancia ya fue eliminada")
        if request.environment is not None and request.environment != target["environment"]:
            raise HTTPException(status_code=409, detail="environment no coincide con la instancia")
        effective_environment = target["environment"]

    if request.command == Command.reconcile:
        target = db.execute(
            "SELECT instance_id, status, operation_id FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="instance_id no existe")
        if target["status"] == "deleted":
            raise HTTPException(status_code=409, detail="la instancia ya fue eliminada")
        source_id = request.source_operation_id
        if source_id:
            source = db.execute(
                "SELECT operation_id, instance_id, status, environment FROM operations WHERE operation_id=?",
                (source_id,),
            ).fetchone()
            if source is None or source["instance_id"] != instance_id:
                raise HTTPException(status_code=404, detail="source_operation_id no pertenece a la instancia")
            if source["status"] != "uncertain":
                raise HTTPException(status_code=409, detail="source_operation_id no está en uncertain")
            effective_environment = source["environment"] or effective_environment
        payload["source_operation_id"] = source_id

    if request.plan_operation_id:
        source = db.execute(
            "SELECT instance_id, environment, plan_id FROM operations WHERE operation_id = ?",
            (request.plan_operation_id,),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="plan_operation_id no existe")
        instance_id = instance_id or source["instance_id"]
        if source["environment"]:
            effective_environment = source["environment"]
            payload["environment"] = effective_environment
        if source["plan_id"] and not request.plan_id:
            payload["plan_id"] = source["plan_id"]
        source_operation = db.execute(
            "SELECT command, status FROM operations WHERE operation_id = ?",
            (request.plan_operation_id,),
        ).fetchone()
        destroyable_plan_statuses = {"succeeded", "failed", "uncertain"}
        valid_source = (
            source_operation is not None
            and source_operation["command"] in {"plan", "provision"}
            and (
                source_operation["status"] == "succeeded"
                or (request.command == Command.destroy and source_operation["status"] in destroyable_plan_statuses)
            )
        )
        if request.command in {Command.apply, Command.destroy} and not valid_source:
            raise HTTPException(status_code=409, detail="el plan todavía no está listo")

    if request.command in {Command.apply, Command.destroy} and not instance_id:
        raise HTTPException(status_code=422, detail="la operacion no tiene instancia objetivo")

    if request.command in {Command.apply, Command.destroy, Command.power, Command.reconcile} and instance_id:
        active = _active_instance_operation(db, instance_id)
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail=f"la instancia ya tiene una operacion activa: {active['operation_id']}",
            )

    payload["instance_id"] = instance_id
    payload["environment"] = effective_environment
    if secret_path:
        payload["secret_ref"] = "operation-secret"

    max_queue_depth = max(0, int(os.getenv("IACP_MAX_QUEUE_DEPTH", "0")))
    if max_queue_depth:
        queued = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE status='pending'"
        ).fetchone()["count"]
        if queued >= max_queue_depth:
            raise HTTPException(
                status_code=429,
                detail="la cola de operaciones está llena; reintente después",
                headers={"Retry-After": "5"},
            )

    plan_id = request.plan_id or payload.get("plan_id")
    db.execute(
        """
        INSERT INTO operations(
          operation_id, command, action, status, payload_json, plan_id, environment,
          secret_path, instance_id, plan_operation_id, created_at
        ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_id,
            request.command.value,
            request.action.value,
            json.dumps(payload),
            plan_id,
            effective_environment,
            secret_path,
            instance_id,
            request.plan_operation_id,
            created,
        ),
    )
    if request.command in {Command.plan, Command.provision} and request.action == Action.provision and request.spec is not None:
        db.execute(
            """
            INSERT INTO instances(
              instance_id, operation_id, name, image, plan_id, environment, status, power_state,
              spec_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?)
            """,
            (
                instance_id,
                operation_id,
                request.spec.name,
                request.spec.image,
                plan_id,
                effective_environment,
                request.spec.model_dump_json(),
                created,
                created,
            ),
        )
    emit(db, operation_id, "operation.created", "pending")
    response = {
        "operation_id": operation_id,
        "instance_id": instance_id,
        "status": "queued" if request.command in {Command.provision, Command.reconcile} else "pending",
        "command": request.command.value,
        "action": request.action.value,
        "environment": effective_environment,
    }
    if request.command in {Command.provision, Command.reconcile}:
        response["job_id"] = operation_id
    return response, operation_id


def create_operation(request: OperationRequest) -> dict:
    with database() as db:
        response, _ = _insert_operation(db, request)
    return response


def create_instance(request: InstanceCreateRequest, idempotency_key: str | None) -> dict:
    spec, plan, _image, password, environment = _resolve_public_request(request)
    body = _request_plain_payload(request)
    body["environment"] = environment
    scope = "POST:/v1/instances"

    def builder(db):
        operation_id = str(uuid.uuid4())
        secret_path = None
        if password is not None:
            secret_path = str(write_secret(operation_id, password))
        operation_request = OperationRequest(
            command=Command.provision,
            action=Action.provision,
            plan_id=plan["plan_id"],
            spec=spec,
            environment=environment,
        )
        try:
            response, created_operation_id = _insert_operation(
                db,
                operation_request,
                secret_path=secret_path,
                operation_id=operation_id,
            )
        except Exception:
            delete_secret(secret_path)
            raise
        return response, created_operation_id

    return _idempotent_mutation(scope=scope, idempotency_key=idempotency_key, body=body, builder=builder)


def create_provision(instance_id: str, plan_operation_id: str | None = None) -> dict:
    with database() as db:
        plan = db.execute(
            """
            SELECT operation_id, plan_id FROM operations
            WHERE instance_id = ? AND command IN ('plan', 'provision')
            ORDER BY created_at DESC LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
    plan_id = plan_operation_id or (plan["operation_id"] if plan else None)
    if plan_id is None:
        raise HTTPException(status_code=409, detail="la instancia no tiene un plan")
    return create_operation(
        OperationRequest(
            command=Command.apply,
            action=Action.provision,
            instance_id=instance_id,
            plan_operation_id=plan_id,
        )
    )


def create_destroy(instance_id: str, idempotency_key: str | None = None) -> dict:
    body = {"instance_id": instance_id, "action": "destroy"}

    def builder(db):
        instance = db.execute(
            "SELECT status, power_state, vmid, environment FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if instance is None:
            raise HTTPException(status_code=404, detail="instance not found")
        if is_protected_vmid(instance["vmid"]):
            raise HTTPException(status_code=409, detail="PROTECTED_INSTANCE")
        if instance["status"] == "deleted":
            raise HTTPException(status_code=409, detail="la instancia ya fue eliminada")
        if instance["power_state"] == "running":
            raise HTTPException(status_code=409, detail="apague la instancia antes de destruirla")
        plan = _latest_provision(db, instance_id)
        # A plan/provision that failed before a VMID was reserved leaves only a
        # logical record. Mark it deleted immediately while preserving an
        # auditable destroy operation; there is no physical VM to reconcile.
        if instance["vmid"] is None and instance["status"] in {"failed", "uncertain"}:
            operation_id = str(uuid.uuid4())
            created = now()
            payload = json.dumps(
                {
                    "command": "destroy",
                    "action": "destroy",
                    "instance_id": instance_id,
                    "environment": instance["environment"],
                    "reason": "logical_cleanup_no_physical_vm",
                },
                sort_keys=True,
            )
            db.execute(
                """
                INSERT INTO operations(
                  operation_id, command, action, status, payload_json, instance_id,
                  environment, created_at, started_at, finished_at, last_heartbeat,
                  exit_code, output, error
                ) VALUES (?, 'destroy', 'destroy', 'succeeded', ?, ?, ?, ?, ?, ?, ?, 0, ?, '')
                """,
                (
                    operation_id,
                    payload,
                    instance_id,
                    instance["environment"],
                    created,
                    created,
                    created,
                    created,
                    "No había VM física; se eliminó el registro lógico fallido.",
                ),
            )
            db.execute(
                "UPDATE instances SET status='deleted', power_state='unknown', updated_at=? WHERE instance_id=?",
                (created, instance_id),
            )
            emit(db, operation_id, "operation.created", "succeeded", "logical cleanup")
            emit(db, operation_id, "logical.delete.completed", "succeeded", "no physical VM")
            return {
                "operation_id": operation_id,
                "job_id": operation_id,
                "instance_id": instance_id,
                "status": "completed",
                "command": "destroy",
                "action": "destroy",
            }, operation_id
        if plan is None:
            raise HTTPException(status_code=409, detail="la instancia no tiene un plan")
        response, operation_id = _insert_operation(
            db,
            OperationRequest(
                command=Command.destroy,
                action=Action.destroy,
                instance_id=instance_id,
                plan_operation_id=plan["operation_id"],
                plan_id=plan["plan_id"],
            ),
        )
        response["status"] = "queued"
        response["job_id"] = operation_id
        return response, operation_id

    return _idempotent_mutation(
        scope=f"DELETE:/v1/instances/{instance_id}",
        idempotency_key=idempotency_key,
        body=body,
        builder=builder,
    )


def create_power(instance_id: str, action: PowerAction, idempotency_key: str | None = None) -> dict:
    body = {"instance_id": instance_id, "power_action": action.value}

    def builder(db):
        response, operation_id = _insert_operation(
            db,
            OperationRequest(
                command=Command.power,
                action=Action.power,
                instance_id=instance_id,
                power_action=action,
            ),
        )
        response["status"] = "queued"
        response["job_id"] = operation_id
        return response, operation_id

    return _idempotent_mutation(
        scope=f"POST:/v1/instances/{instance_id}/power/{action.value}",
        idempotency_key=idempotency_key,
        body=body,
        builder=builder,
    )


def create_reconcile(
    instance_id: str,
    source_operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    body = {
        "instance_id": instance_id,
        "source_operation_id": source_operation_id,
        "action": "reconcile",
    }

    def builder(db):
        instance = db.execute(
            "SELECT status FROM instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
        if instance is None:
            raise HTTPException(status_code=404, detail="instance not found")
        if instance["status"] == "deleted":
            raise HTTPException(status_code=409, detail="la instancia ya fue eliminada")
        selected = source_operation_id
        if selected is None:
            row = db.execute(
                """
                SELECT operation_id FROM operations
                WHERE instance_id=? AND status='uncertain'
                ORDER BY created_at DESC LIMIT 1
                """,
                (instance_id,),
            ).fetchone()
            selected = row["operation_id"] if row else None
        if selected is None:
            raise HTTPException(status_code=409, detail="la instancia no tiene una operación uncertain para reconciliar")
        source = db.execute(
                "SELECT * FROM operations WHERE operation_id=? AND instance_id=?",
            (selected, instance_id),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="source_operation_id no pertenece a la instancia")
        if source["status"] != "uncertain":
            raise HTTPException(status_code=409, detail="source_operation_id no está en uncertain")
        response, operation_id = _insert_operation(
            db,
            OperationRequest(
                command=Command.reconcile,
                action=Action.reconcile,
                instance_id=instance_id,
                source_operation_id=selected,
                environment=source["environment"] or "lab",
            ),
        )
        response["status"] = "queued"
        response["job_id"] = operation_id
        response["source_operation_id"] = selected
        return response, operation_id

    return _idempotent_mutation(
        scope=f"POST:/v1/instances/{instance_id}/reconcile",
        idempotency_key=idempotency_key,
        body=body,
        builder=builder,
    )


def operation_row(row):
    if row is None:
        raise HTTPException(status_code=404, detail="operation not found")
    result = dict(row)
    raw_status = result["status"]
    result["job_id"] = result["operation_id"]
    result["internal_status"] = raw_status
    result["timings"] = operation_timings(result)
    result["status"] = PUBLIC_OPERATION_STATUS.get(raw_status, raw_status)
    result["normalized_status"] = result["status"]
    result["payload"] = _public_payload(result.pop("payload_json"))
    result["error_code"] = public_error_code(result.get("error"), raw_status)
    result["output"] = public_operation_output({**result, "payload": result["payload"], "status": raw_status})
    result["error"] = public_operation_error({**result, "status": raw_status})
    result.pop("secret_path", None)
    result.pop("target_node", None)
    result.pop("worker_id", None)
    result.pop("workspace_path", None)
    result.pop("plan_path", None)
    with database() as db:
        result["latest_event"] = latest_event(db, result["operation_id"])
    return result


def _latest_operation(db, instance_id: str):
    return db.execute(
        "SELECT * FROM operations WHERE instance_id = ? ORDER BY created_at DESC LIMIT 1",
        (instance_id,),
    ).fetchone()


def instance_row(row):
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    result = dict(row)
    result["spec"] = json.loads(result.pop("spec_json"))
    result["image_id"] = result.get("image")
    result.pop("node", None)
    result["status"] = public_instance_status(result)
    with database() as db:
        latest = _latest_operation(db, result["instance_id"])
    if latest:
        latest_result = dict(latest)
        raw_latest_status = latest_result["status"]
        latest_result["job_id"] = latest_result["operation_id"]
        latest_result["internal_status"] = raw_latest_status
        latest_result["status"] = PUBLIC_OPERATION_STATUS.get(raw_latest_status, raw_latest_status)
        latest_result["normalized_status"] = latest_result["status"]
        latest_result["payload"] = _public_payload(latest_result.pop("payload_json"))
        latest_result["error_code"] = public_error_code(latest_result.get("error"), raw_latest_status)
        latest_result["output"] = public_operation_output({**latest_result, "status": raw_latest_status})
        latest_result["error"] = public_operation_error({**latest_result, "status": raw_latest_status})
        latest_result.pop("secret_path", None)
        latest_result.pop("workspace_path", None)
        latest_result.pop("plan_path", None)
        result["latest_operation"] = latest_result
    else:
        result["latest_operation"] = None
    return result


def list_instances(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    name: str | None = None,
    include_deleted: bool = False,
):
    normalized_status = (status or "").strip().lower()
    all_statuses = normalized_status in {"all", "*"}
    filters = ["1 = 1"]
    values: list[object] = []
    if normalized_status == "ready":
        filters.extend(["i.status = 'ready'", "i.power_state = 'running'"])
    elif normalized_status == "stopped":
        filters.extend(["i.status = 'ready'", "i.power_state = 'stopped'"])
    else:
        internal_status = None if all_statuses else next(
            (key for key, value in PUBLIC_INSTANCE_STATUS.items() if value == normalized_status),
            normalized_status,
        )
    if normalized_status not in {"ready", "stopped"} and internal_status:
        filters.append("i.status = ?")
        values.append(internal_status)
    elif normalized_status not in {"ready", "stopped"} and not include_deleted:
        if all_statuses:
            filters.append("i.status != 'deleted'")
        else:
            filters.append("i.status NOT IN ('deleted', 'failed', 'uncertain')")
    if name:
        filters.append("i.name = ?")
        values.append(name)
    where = " AND ".join(filters)
    with database() as db:
        total = db.execute(
            f"SELECT COUNT(*) AS count FROM instances i WHERE {where}", values
        ).fetchone()["count"]
        rows = db.execute(
            f"""
            SELECT i.* FROM instances i
            WHERE {where}
            ORDER BY i.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (*values, limit, offset),
        ).fetchall()
    return {"instances": [instance_row(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def list_operations(
    *,
    limit: int = 50,
    offset: int = 0,
    instance_id: str | None = None,
    status: str | None = None,
):
    internal_status = next((key for key, value in PUBLIC_OPERATION_STATUS.items() if value == status), status)
    filters = ["1 = 1"]
    values: list[object] = []
    if instance_id:
        filters.append("instance_id = ?")
        values.append(instance_id)
    if internal_status:
        filters.append("status = ?")
        values.append(internal_status)
    where = " AND ".join(filters)
    with database() as db:
        total = db.execute(
            f"SELECT COUNT(*) AS count FROM operations WHERE {where}", values
        ).fetchone()["count"]
        rows = db.execute(
            f"""
            SELECT * FROM operations
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (*values, limit, offset),
        ).fetchall()
    return {"operations": [operation_row(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def plan_catalog_response(environment: str | None = None) -> list[dict]:
    try:
        selected = resolve_environment(environment)
    except EnvironmentError as error:
        raise HTTPException(status_code=error.status_code, detail=error.code) from error
    return plan_catalog(selected)


def image_catalog(environment: str | None = None) -> list[dict]:
    try:
        selected = resolve_environment(environment)
    except EnvironmentError as error:
        raise HTTPException(status_code=error.status_code, detail=error.code) from error
    return catalog_images(selected)
