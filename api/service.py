import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
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
from api.environments import EnvironmentConfigurationError, resolve_environment
from api.models import Action, Command, CommercialContext, InstanceCreateRequest, InstanceSpec, OperationRequest, PowerAction
from api.protection import is_protected_vmid
from api.secrets import delete_secret, write_secret
from api.network_policy import apply_to_spec, selected_profile


_MUTATION_CLEANUPS: ContextVar[list[Callable[[], None]] | None] = ContextVar(
    "iacp_mutation_cleanups", default=None
)
_LOGGER = logging.getLogger(__name__)


def register_idempotent_rollback_cleanup(callback: Callable[[], None]) -> None:
    callbacks = _MUTATION_CLEANUPS.get()
    if callbacks is not None:
        callbacks.append(callback)


def _idempotency_commit_state(scope: str, key: str) -> bool | None:
    """Devuelve None si no se pudo resolver una respuesta ambigua de COMMIT."""

    try:
        with database() as db:
            return db.execute(
                "SELECT 1 FROM idempotency_keys WHERE scope=? AND idempotency_key=?",
                (scope, key),
            ).fetchone() is not None
    except Exception:
        return None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reject_protected_vmid(vmid: object) -> None:
    if is_protected_vmid(vmid):
        raise HTTPException(status_code=409, detail="PROTECTED_INSTANCE")


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
    "uncertain",
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
    "ipam.address.reserved": "network.address.reserved",
    "ipam.address.released": "network.address.released",
    "ipam.release.pending": "network.address.release_pending",
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
    "POWER_STACK_UNSUPPORTED": "POWER_TARGET_UNSUPPORTED",
    "PROXMOX_POOL_REQUIRED": "RESOURCE_POOL_REQUIRED",
    "PROXMOX_POOL_NOT_FOUND": "RESOURCE_POOL_NOT_FOUND",
    "VMID_RANGE_EXHAUSTED": "VMID_ALLOCATION_EXHAUSTED",
    "SSH_PUBLIC_KEY_REQUIRED": "SSH_PUBLIC_KEY_REQUIRED",
    "RECONCILIATION_REQUIRED": "RECONCILIATION_REQUIRED",
    "RECONCILIATION_PHYSICAL_RESOURCE_ABSENT": "RECONCILIATION_PHYSICAL_RESOURCE_ABSENT",
    "PHYSICAL_IDENTITY_MISMATCH": "PHYSICAL_IDENTITY_MISMATCH",
    "PHYSICAL_NODE_MISMATCH": "PHYSICAL_NODE_MISMATCH",
    "IPAM_NOT_CONFIGURED": "IPAM_NOT_CONFIGURED",
    "IPAM_CONFIGURATION_INVALID": "IPAM_CONFIGURATION_INVALID",
    "IPAM_PREFIX_INVALID": "IPAM_PREFIX_INVALID",
    "IPAM_PREFIX_POOL_UNSAFE": "IPAM_PREFIX_POOL_UNSAFE",
    "IPAM_GATEWAY_NOT_FOUND": "IPAM_GATEWAY_NOT_FOUND",
    "IPAM_GATEWAY_NOT_RESERVED": "IPAM_GATEWAY_NOT_RESERVED",
    "IPAM_POOL_EXHAUSTED": "IPAM_POOL_EXHAUSTED",
    "IPAM_ADDRESS_CONFLICT": "IPAM_ADDRESS_CONFLICT",
    "IPAM_ALLOCATION_CONFLICT": "IPAM_ALLOCATION_CONFLICT",
    "IPAM_RESPONSE_INVALID": "IPAM_RESPONSE_INVALID",
    "IPAM_TAG_NOT_FOUND": "IPAM_TAG_NOT_FOUND",
    "IPAM_PERMISSION_DENIED": "IPAM_PERMISSION_DENIED",
    "IPAM_UNAVAILABLE": "IPAM_SERVICE_UNAVAILABLE",
    "IPAM_REQUEST_UNCERTAIN": "IPAM_OPERATION_UNCERTAIN",
    "IPAM_IDENTITY_MISMATCH": "IPAM_IDENTITY_MISMATCH",
    "IPAM_RELEASE_PENDING": "IPAM_RELEASE_PENDING",
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
    "netbox",
    "authorization: bearer",
    "nbt_",
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


def _active_instance_operation(
    db,
    instance_id: str,
    *,
    allow_queued_service: bool = False,
    ignore_operation_id: str | None = None,
    ignore_uncertain_reconcile_source_id: str | None = None,
):
    from api.applications import guard_instance
    guard_instance(db, instance_id, allow_queued=allow_queued_service)
    placeholders = ",".join("?" for _ in ACTIVE_OPERATION_STATUSES)
    return db.execute(
        f"""
        SELECT operation_id, command, action, status
        FROM operations
        WHERE instance_id = ? AND status IN ({placeholders})
          AND (CAST(? AS TEXT) IS NULL OR operation_id <> ?)
          AND NOT (
            command='reconcile' AND status='uncertain'
            AND CAST(? AS TEXT) IS NOT NULL
            AND payload_json::jsonb ->> 'source_operation_id' = ?
          )
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (
            instance_id,
            *sorted(ACTIVE_OPERATION_STATUSES),
            ignore_operation_id,
            ignore_operation_id,
            ignore_uncertain_reconcile_source_id,
            ignore_uncertain_reconcile_source_id,
        ),
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


def _commercial_json(context: CommercialContext | dict | None) -> str:
    if context is None:
        return "{}"
    value = context.model_dump(mode="json") if isinstance(context, CommercialContext) else context
    return json.dumps(value, sort_keys=True)


def _commercial_dict(value: str | None) -> dict | None:
    if not value:
        return None
    parsed = json.loads(value)
    return parsed or None


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
    message = {
        "IPAM_GATEWAY_NOT_FOUND": "No se encontró en NetBox la dirección exacta del gateway.",
        "IPAM_ALLOCATION_CONFLICT": "NetBox no confirmó una reserva única para la instancia.",
    }.get(code, "La operación no pudo confirmarse.")
    return f"{message} Código: {code}."


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
        "access": access,
        "commercial_context": (
            request.commercial_context.model_dump(mode="json")
            if request.commercial_context is not None
            else None
        ),
        "network_profile": selected_profile(request.network_profile),
        "tags": list(request.tags),
        "start_on_create": request.start_on_create,
    }


_IDEMPOTENCY_HASH_VERSION = "hmac-sha256-v1:"
_LEGACY_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


def initialize_idempotency() -> bytes:
    """Lee o crea la clave del stack, con publicación atómica en workdata."""

    root = Path(os.getenv("IACP_WORKDATA_DIR", "/var/lib/iacp-controlplane"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / ".idempotency-hmac-v1"
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=root, prefix=".idempotency-key-", delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(secrets.token_bytes(32))
                file.flush()
                os.fsync(file.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass  # Otro proceso publicó la clave de este mismo volumen.
                else:
                    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            finally:
                temporary.unlink()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        mode = os.fstat(file.fileno()).st_mode
        key = file.read(33) if stat.S_ISREG(mode) and not mode & 0o077 else b""
    if len(key) != 32:
        raise RuntimeError("clave de idempotencia inválida")
    return key


def _body_fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _keyed_fingerprint(secret: bytes, scope: str, key: str, digest: str) -> str:
    message = json.dumps([_IDEMPOTENCY_HASH_VERSION, scope, key, digest], separators=(",", ":")).encode()
    return _IDEMPOTENCY_HASH_VERSION + hmac.new(secret, message, hashlib.sha256).hexdigest()


def migrate_idempotency_fingerprints(*, limit: int = 250) -> int:
    """Convierte un lote tras aprobar la transición sin reescribir al arrancar."""

    if not 1 <= limit <= 1000:
        raise ValueError("el tamaño del lote debe estar entre 1 y 1000")
    secret = initialize_idempotency()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """
            SELECT scope, idempotency_key, request_hash FROM idempotency_keys
            WHERE request_hash ~ '^[0-9a-f]{64}$'
            ORDER BY created_at, scope, idempotency_key
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            scope = row["scope"]
            key = row["idempotency_key"]
            digest = _keyed_fingerprint(secret, scope, key, row["request_hash"])
            db.execute(
                "UPDATE idempotency_keys SET request_hash=? WHERE scope=? AND idempotency_key=?",
                (digest, scope, key),
            )
        db.execute("COMMIT")
    return len(rows)


def _idempotency_key_or_error(value: str | None) -> str:
    key = (value or "").strip()
    if not key:
        return f"generated-{uuid.uuid4()}"
    if len(key) > 255:
        raise HTTPException(status_code=422, detail="Idempotency-Key no puede superar 255 caracteres")
    return key


def _existing_idempotency(db, scope: str, key: str, request_hash: str, legacy_hash: str):
    row = db.execute(
        "SELECT request_hash, response_json FROM idempotency_keys WHERE scope=? AND idempotency_key=?",
        (scope, key),
    ).fetchone()
    if row is None:
        return None
    stored = row["request_hash"]
    expected = legacy_hash if _LEGACY_FINGERPRINT.fullmatch(stored) else request_hash
    if not hmac.compare_digest(stored.encode(), expected.encode()):
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
    legacy_hash = _body_fingerprint(body)
    request_hash = _keyed_fingerprint(initialize_idempotency(), scope, key, legacy_hash)
    cleanups: list[Callable[[], None]] = []
    token = _MUTATION_CLEANUPS.set(cleanups)
    commit_attempted = False
    try:
        with database() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                existing = _existing_idempotency(db, scope, key, request_hash, legacy_hash)
                if existing is not None:
                    commit_attempted = True
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
                commit_attempted = True
                db.execute("COMMIT")
                return response
            except Exception:
                try:
                    db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
    except Exception:
        commit_state = _idempotency_commit_state(scope, key) if commit_attempted else False
        if commit_state is False:
            for cleanup in reversed(cleanups):
                try:
                    cleanup()
                except OSError:
                    _LOGGER.exception("Falló la compensación local de una mutación")
        elif commit_attempted:
            # La fila puede haberse confirmado aunque se perdiera su respuesta.
            # Conservar el secreto evita dejar una operación persistida sin el
            # archivo que necesita el worker.
            _LOGGER.warning(
                "Resultado de COMMIT ambiguo para una mutación idempotente; se conserva el secreto hasta reconciliarla"
            )
        raise
    finally:
        _MUTATION_CLEANUPS.reset(token)


def _image_for_plan(image_id: str, plan: dict) -> dict:
    image = image_by_id(image_id)
    if image is None:
        raise HTTPException(status_code=422, detail=f"image_id no disponible: {image_id}")
    if plan["vcpus"] < image.get("minimum_vcpus", 1):
        raise HTTPException(status_code=422, detail="el plan no cumple el mínimo de vCPU de la imagen")
    if plan["memory_mb"] < image.get("minimum_memory_mb", 0):
        raise HTTPException(status_code=422, detail="el plan no cumple el mínimo de memoria de la imagen")
    if plan["disk_gb"] < image.get("minimum_disk_gb", 0):
        raise HTTPException(status_code=422, detail="el plan no cumple el mínimo de disco de la imagen")
    return image


def _operator_spec(spec: InstanceSpec, plan_id: str | None) -> tuple[InstanceSpec, str]:
    fields = ("vcpus", "memory_mb", "disk_gb")
    if plan_id is None:
        # El contrato anterior permite omitir plan_id para plan. Si indicó
        # los recursos, deben corresponder a un plan del mismo catálogo.
        specified = spec.model_fields_set.intersection(fields)
        if not specified:
            plan_id = "kvm-1"
        else:
            matching = [
                plan["plan_id"] for plan in plan_catalog()
                if all(plan[field] == getattr(spec, field) for field in specified)
            ]
            if len(matching) == 1:
                plan_id = matching[0]
    plan = plan_by_id(plan_id) if plan_id is not None else None
    if plan is None:
        raise HTTPException(422, "plan_id no disponible")
    if any(field in spec.model_fields_set and getattr(spec, field) != plan[field] for field in fields):
        raise HTTPException(422, "los recursos no coinciden con plan_id")
    _image_for_plan(spec.image, plan)
    return apply_to_spec(spec.model_copy(update={field: plan[field] for field in fields})), plan["plan_id"]


def _resolve_public_request(
    request: InstanceCreateRequest,
) -> tuple[InstanceSpec, dict, dict, str | None, str]:
    try:
        environment = resolve_environment(request.environment)
    except EnvironmentConfigurationError as error:
        raise HTTPException(status_code=error.status_code, detail=error.code) from error
    plan = plan_by_id(request.plan_id)
    if plan is None:
        raise HTTPException(status_code=422, detail=f"plan_id no disponible: {request.plan_id}")
    image = _image_for_plan(request.image_id, plan)
    resolved_profile = selected_profile(request.network_profile)
    profile = network_profile_by_id(resolved_profile)
    if profile is None:
        raise HTTPException(status_code=422, detail="network_profile no disponible")
    if not profile["enabled"]:
        raise HTTPException(status_code=503, detail="NETWORK_PROFILE_NOT_CONFIGURED")
    tags = ([request.commercial_context.customer_tag] if request.commercial_context else [])
    tags.extend(request.tags)
    spec = InstanceSpec(
        name=request.name,
        image=request.image_id,
        vcpus=plan["vcpus"],
        memory_mb=plan["memory_mb"],
        disk_gb=plan["disk_gb"],
        network_profile=resolved_profile,
        network_mode="static" if resolved_profile == "prod_static" else "dhcp",
        dns_servers=["1.1.1.1", "8.8.8.8"] if resolved_profile == "prod_static" else [],
        ssh_public_keys=request.access.ssh_public_keys,
        started=request.start_on_create,
        tags=tags,
    )
    password = (
        request.access.root_password.get_secret_value()
        if request.access.root_password is not None
        else None
    )
    return spec, plan, image, password, environment


def _operation_target(db, instance_id: str):
    row = db.execute(
        "SELECT * FROM instances WHERE instance_id=? FOR UPDATE",
        (instance_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "instance_id no existe")
    _reject_protected_vmid(row["vmid"])
    return row


def _check_destroyable(instance) -> None:
    if instance["status"] == "deleted":
        raise HTTPException(409, "la instancia ya fue eliminada")
    if instance["power_state"] == "running":
        raise HTTPException(409, "apague la instancia antes de destruirla")


def _plan_source(db, plan_operation_id: str):
    source = db.execute(
        "SELECT * FROM operations WHERE operation_id=?",
        (plan_operation_id,),
    ).fetchone()
    if source is None:
        raise HTTPException(404, "plan_operation_id no existe")
    return source


def _reconciliation_source(db, instance, source_id: str | None):
    instance_id = instance["instance_id"]
    if source_id:
        source = db.execute(
            "SELECT * FROM operations WHERE operation_id=? AND instance_id=?",
            (source_id, instance_id),
        ).fetchone()
        if source is None:
            raise HTTPException(404, "source_operation_id no pertenece a la instancia")
        if source["status"] != "uncertain":
            raise HTTPException(409, "source_operation_id no está en uncertain")
        if source["command"] == "reconcile":
            raise HTTPException(409, "seleccione la operación incierta de origen")
    else:
        source = db.execute(
            """
            SELECT * FROM operations
            WHERE instance_id=? AND status='uncertain' AND command <> 'reconcile'
            ORDER BY created_at DESC, operation_id DESC LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
        if source is None:
            raise HTTPException(409, "la instancia no tiene una operación uncertain para reconciliar")
    if instance["status"] == "deleted" and not _deleted_reconcile_source_allowed(db, instance_id, source):
        raise HTTPException(
            409,
            "una operación de energía solo puede cerrarse después de confirmar la eliminación física"
            if source["command"] == "power"
            else "la instancia eliminada solo admite reconciliación de operaciones físicas inciertas",
        )
    return source


def _insert_operation(
    db,
    request: OperationRequest,
    *,
    commercial_context: dict | None = None,
    secret_path: str | None = None,
    operation_id: str | None = None,
    environment: str | None = None,
) -> tuple[dict, str]:
    operation_id = operation_id or str(uuid.uuid4())
    try:
        environment = resolve_environment(environment or request.environment)
    except EnvironmentConfigurationError as error:
        raise HTTPException(status_code=error.status_code, detail=error.code) from error
    command = request.command
    effective_stack = "mng01"
    created = now()
    instance_id = request.instance_id
    plan_operation_id = request.plan_operation_id
    plan_id = request.plan_id
    spec = request.spec
    payload = request.model_dump(mode="json")

    if command in {Command.plan, Command.provision}:
        instance_id = str(uuid.uuid4())
        spec, plan_id = _operator_spec(spec, plan_id)
        payload["spec"] = spec.model_dump(mode="json")
        duplicate = db.execute(
            "SELECT instance_id FROM instances WHERE name=? AND status <> 'deleted' LIMIT 1",
            (spec.name,),
        ).fetchone()
        if duplicate is not None:
            raise HTTPException(409, "ya existe una instancia con ese nombre")
    else:
        source = _plan_source(db, plan_operation_id) if plan_operation_id else None
        if source is not None:
            if not source["instance_id"] or (instance_id and instance_id != source["instance_id"]):
                raise HTTPException(409, "plan_operation_id no pertenece a la instancia")
            instance_id = source["instance_id"]
        target = _operation_target(db, instance_id)

        if command in {Command.apply, Command.destroy}:
            if source is None:
                source = _plan_source(db, target["operation_id"])
            if source["operation_id"] != target["operation_id"] or source["instance_id"] != instance_id:
                raise HTTPException(409, "plan_operation_id no es el plan vigente de la instancia")
            _reject_protected_vmid(source["vmid"])
            if command == Command.apply:
                if target["status"] != "planned" or source["command"] != "plan" or source["status"] != "succeeded":
                    raise HTTPException(409, "el plan todavía no está listo o ya se aplicó")
                source_spec = json.loads(source["payload_json"]).get("spec") or {}
                profile = network_profile_by_id(selected_profile(source_spec.get("network_profile", "lab_dhcp")))
                if profile is None or not profile["enabled"]:
                    raise HTTPException(503, "NETWORK_PROFILE_NOT_CONFIGURED")
            else:
                _check_destroyable(target)
                if source["command"] not in {"plan", "provision"} or source["status"] not in {"succeeded", "failed", "uncertain"}:
                    raise HTTPException(409, "el plan todavía no está listo")
            if plan_id is not None and plan_id != source["plan_id"]:
                raise HTTPException(409, "plan_id no coincide con el plan de la instancia")
            plan_operation_id = source["operation_id"]
            plan_id = source["plan_id"]
            effective_stack = source["stack"] or effective_stack
        elif command == Command.power:
            if target["status"] == "deleted":
                raise HTTPException(409, "la instancia ya fue eliminada")
            source = _plan_source(db, target["operation_id"])
            if source["stack"] not in {"mng01", "local"}:
                raise HTTPException(409, "cluster no soportado")
            effective_stack = source["stack"]
        else:
            source = _reconciliation_source(db, target, request.source_operation_id)
            effective_stack = source["stack"] or effective_stack
            payload["source_operation_id"] = source["operation_id"]

        active = _active_instance_operation(
            db, instance_id,
            allow_queued_service=command == Command.reconcile
            or (command == Command.power and request.power_action == PowerAction.start),
            ignore_operation_id=(source["operation_id"] if command == Command.reconcile else None),
            ignore_uncertain_reconcile_source_id=(
                source["operation_id"] if command == Command.reconcile else None
            ),
        )
        if active is not None:
            raise HTTPException(409, f"la instancia ya tiene una operacion activa: {active['operation_id']}")

    payload["instance_id"] = instance_id
    payload["environment"] = environment
    payload["plan_operation_id"] = plan_operation_id
    payload["plan_id"] = plan_id
    payload["stack"] = effective_stack
    if secret_path:
        payload["secret_ref"] = "operation-secret"

    max_queue_depth = int(os.getenv("IACP_MAX_QUEUE_DEPTH", "100"))
    if max_queue_depth <= 0:
        max_queue_depth = 100
    queued = db.execute(
        "SELECT COUNT(*) AS count FROM operations WHERE status='pending'"
    ).fetchone()["count"]
    if queued >= max_queue_depth:
        raise HTTPException(
            status_code=429,
            detail="la cola de operaciones está llena; reintente después",
            headers={"Retry-After": "5"},
        )

    context_json = _commercial_json(commercial_context)
    db.execute(
        """
        INSERT INTO operations(
          operation_id, command, action, status, payload_json, plan_id, commercial_context_json,
          secret_path, instance_id, plan_operation_id, stack, environment, created_at
        ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_id,
            request.command.value,
            request.action.value,
            json.dumps(payload),
            plan_id,
            context_json,
            secret_path,
            instance_id,
            plan_operation_id,
            effective_stack,
            environment,
            created,
        ),
    )
    if command in {Command.plan, Command.provision}:
        db.execute(
            """
            INSERT INTO instances(
              instance_id, operation_id, name, image, plan_id, environment, status, power_state,
              spec_json, commercial_context_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?)
            """,
            (
                instance_id,
                operation_id,
                spec.name,
                spec.image,
                plan_id,
                environment,
                spec.model_dump_json(),
                context_json,
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
        "environment": environment,
    }
    if request.command in {Command.provision, Command.reconcile}:
        response["job_id"] = operation_id
    return response, operation_id


def create_operation(
    request: OperationRequest, idempotency_key: str | None = None
) -> dict:
    if idempotency_key is not None:
        if not idempotency_key.strip():
            raise HTTPException(422, "Idempotency-Key es obligatorio")

        def builder(db):
            return _insert_operation(db, request)

        return _idempotent_mutation(
            scope="POST:/v1/operations",
            idempotency_key=idempotency_key,
            body=request.model_dump(mode="json"),
            builder=builder,
        )
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        response, _ = _insert_operation(db, request)
        db.execute("COMMIT")
    return response


def create_instance(request: InstanceCreateRequest, idempotency_key: str | None) -> dict:
    spec, plan, _image, password, environment = _resolve_public_request(request)
    context = request.commercial_context.model_dump(mode="json") if request.commercial_context else None
    body = _request_plain_payload(request)
    scope = "POST:/v1/instances"

    def builder(db):
        operation_id = str(uuid.uuid4())
        secret_path = None
        if password is not None:
            secret_path = str(write_secret(operation_id, password))
            register_idempotent_rollback_cleanup(lambda: delete_secret(secret_path))
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
                commercial_context=context,
                secret_path=secret_path,
                operation_id=operation_id,
                environment=environment,
            )
        except Exception:
            delete_secret(secret_path)
            raise
        return response, created_operation_id

    return _idempotent_mutation(scope=scope, idempotency_key=idempotency_key, body=body, builder=builder)


def create_provision(instance_id: str, plan_operation_id: str | None = None) -> dict:
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        target = _operation_target(db, instance_id)
        response, _ = _insert_operation(
            db,
            OperationRequest(
                command=Command.apply,
                action=Action.provision,
                instance_id=instance_id,
                plan_operation_id=plan_operation_id or target["operation_id"],
            ),
        )
        db.execute("COMMIT")
    return response


def _deleted_reconcile_source_allowed(db, instance_id: str, source) -> bool:
    command = source["command"]
    if command in {"plan", "apply", "provision", "destroy"}:
        return True
    if command != "power":
        return False
    # A later successful destroy means the older power request can no longer
    # affect a VM. The worker still confirms physical absence before closing it.
    destroy = db.execute(
        """
        SELECT 1 FROM operations
        WHERE instance_id=? AND command='destroy' AND status='succeeded'
          AND created_at > ?
        LIMIT 1
        """,
        (instance_id, source["created_at"]),
    ).fetchone()
    return destroy is not None


def create_destroy(instance_id: str, idempotency_key: str | None = None) -> dict:
    body = {"instance_id": instance_id, "action": "destroy"}

    def builder(db):
        instance = _operation_target(db, instance_id)
        _check_destroyable(instance)
        active = _active_instance_operation(db, instance_id)
        if active is not None:
            raise HTTPException(409, f"la instancia ya tiene una operacion activa: {active['operation_id']}")
        plan = _plan_source(db, instance["operation_id"])
        # El cierre lógico solo es seguro si nunca se reservó una identidad
        # física y no queda intención de asignación pendiente de reconciliar.
        if instance["vmid"] is None and instance["status"] in {"failed", "uncertain"}:
            held = db.execute(
                """
                SELECT 1 FROM vmid_leases WHERE operation_id=? AND status='held'
                UNION ALL
                SELECT 1 FROM ip_allocations WHERE instance_id=? AND status <> 'released'
                LIMIT 1
                """,
                (instance["operation_id"], instance_id),
            ).fetchone()
            if held is not None or plan["vmid"] is not None:
                raise HTTPException(409, "la instancia requiere reconciliación")
            operation_id = str(uuid.uuid4())
            created = now()
            payload = json.dumps(
                {
                    "command": "destroy",
                    "action": "destroy",
                    "instance_id": instance_id,
                    "reason": "logical_cleanup_no_physical_vm",
                },
                sort_keys=True,
            )
            db.execute(
                """
                INSERT INTO operations(
                  operation_id, command, action, status, payload_json, instance_id,
                  stack, created_at, started_at, finished_at, last_heartbeat,
                  exit_code, output, error
                ) VALUES (?, 'destroy', 'destroy', 'succeeded', ?, ?, 'mng01', ?, ?, ?, ?, 0, ?, '')
                """,
                (
                    operation_id,
                    payload,
                    instance_id,
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
        response, operation_id = _insert_operation(
            db,
            OperationRequest(
                command=Command.destroy,
                action=Action.destroy,
                instance_id=instance_id,
                plan_operation_id=plan["operation_id"],
                plan_id=plan["plan_id"],
            ),
            commercial_context=_commercial_dict(plan["commercial_context_json"]),
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
        provision = _latest_provision(db, instance_id)
        context = _commercial_dict(provision["commercial_context_json"]) if provision else None
        response, operation_id = _insert_operation(
            db,
            OperationRequest(
                command=Command.power,
                action=Action.power,
                instance_id=instance_id,
                power_action=action,
            ),
            commercial_context=context,
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
        instance = _operation_target(db, instance_id)
        source = _reconciliation_source(db, instance, source_operation_id)
        selected = source["operation_id"]
        response, operation_id = _insert_operation(
            db,
            OperationRequest(
                command=Command.reconcile,
                action=Action.reconcile,
                instance_id=instance_id,
                source_operation_id=selected,
            ),
            commercial_context=_commercial_dict(source["commercial_context_json"]),
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


_UNSET = object()


def operation_row(row, *, prefetched_event=_UNSET):
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
    result["commercial_context"] = _commercial_dict(result.pop("commercial_context_json", "{}"))
    result.pop("secret_path", None)
    result.pop("workspace_path", None)
    result.pop("plan_path", None)
    if prefetched_event is not _UNSET:
        result["latest_event"] = prefetched_event
    else:
        with database() as db:
            result["latest_event"] = latest_event(db, result["operation_id"])
    return result


def _prefetch_instance_details(db, rows) -> dict:
    """Carga en tres consultas lo que ``instance_row`` pedía por fila.

    Antes un listado de 200 instancias ejecutaba 600 consultas, cada una con
    su propio préstamo de conexión del pool.
    """

    instance_ids = [row["instance_id"] for row in rows]
    operation_ids = [row["operation_id"] for row in rows if row["operation_id"]]
    if not instance_ids:
        return {"allocations": {}, "latest": {}, "stacks": {}}
    allocations = {
        row["instance_id"]: {key: row[key] for key in row if key != "instance_id"}
        for row in db.execute(
            """
            SELECT DISTINCT ON (instance_id)
                   instance_id, provider, profile, address, gateway, status, created_at, released_at
            FROM ip_allocations
            WHERE instance_id = ANY(?)
            ORDER BY instance_id, created_at DESC
            """,
            (instance_ids,),
        ).fetchall()
    }
    latest = {
        row["instance_id"]: row
        for row in db.execute(
            """
            SELECT DISTINCT ON (instance_id) *
            FROM operations
            WHERE instance_id = ANY(?)
            ORDER BY instance_id, created_at DESC
            """,
            (instance_ids,),
        ).fetchall()
    }
    stacks = {}
    if operation_ids:
        stacks = {
            row["operation_id"]: row["stack"]
            for row in db.execute(
                "SELECT operation_id, stack FROM operations WHERE operation_id = ANY(?)",
                (operation_ids,),
            ).fetchall()
        }
    return {"allocations": allocations, "latest": latest, "stacks": stacks}


def _prefetch_latest_events(db, operation_ids: list[str]) -> dict:
    if not operation_ids:
        return {}
    rows = db.execute(
        """
        SELECT DISTINCT ON (e.operation_id)
               e.*, o.command, o.action, o.instance_id, o.worker_id,
               i.name AS instance_name
        FROM events e
        LEFT JOIN operations o ON o.operation_id = e.operation_id
        LEFT JOIN instances i ON i.instance_id = o.instance_id
        WHERE e.operation_id = ANY(?)
        ORDER BY e.operation_id, e.event_id DESC
        """,
        (operation_ids,),
    ).fetchall()
    return {row["operation_id"]: event_row(db, row) for row in rows}


def instance_row(row, *, prefetched: dict | None = None):
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    result = dict(row)
    result["spec"] = json.loads(result.pop("spec_json"))
    result["image_id"] = result.get("image")
    result["commercial_context"] = _commercial_dict(result.pop("commercial_context_json", "{}"))
    result["status"] = public_instance_status(result)
    if prefetched is None:
        with database() as db:
            prefetched = _prefetch_instance_details(db, [row])
    allocation = prefetched["allocations"].get(result["instance_id"])
    if allocation is None and result["spec"].get("network_profile") == "prod_static":
        allocation = {
            "provider": "netbox",
            "profile": "prod_static",
            "address": result["spec"].get("ipv4_cidr"),
            "gateway": result["spec"].get("ipv4_gateway"),
            "status": "pending",
        }
    result["network_allocation"] = allocation
    latest = prefetched["latest"].get(result["instance_id"])
    stack_value = prefetched["stacks"].get(result["operation_id"])
    stack = {"stack": stack_value} if stack_value else None
    result["cluster"] = "mng"
    result["stack"] = stack["stack"] if stack else "mng01"
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
        latest_result.pop("commercial_context_json", None)
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
            ORDER BY i.created_at DESC, i.instance_id DESC
            LIMIT ? OFFSET ?
            """,
            (*values, limit, offset),
        ).fetchall()
        prefetched = _prefetch_instance_details(db, rows)
    return {
        "instances": [instance_row(row, prefetched=prefetched) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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
            ORDER BY created_at DESC, operation_id DESC
            LIMIT ? OFFSET ?
            """,
            (*values, limit, offset),
        ).fetchall()
        events = _prefetch_latest_events(db, [row["operation_id"] for row in rows])
    return {
        "operations": [
            operation_row(row, prefetched_event=events.get(row["operation_id"])) for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def plan_catalog_response() -> list[dict]:
    return plan_catalog()


def image_catalog() -> list[dict]:
    return catalog_images()
