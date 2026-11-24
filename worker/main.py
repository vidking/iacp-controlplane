import argparse
import contextvars
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from functools import partial

from api.catalog import cluster_nodes, load_catalog
from api.db import database, initialize
from api.protection import is_protected_vmid
from api.secrets import delete_secret, read_secret
from worker.allocation import allocate, release
from worker.golden import configured_pool_id, observe, validate_pool, variables as golden_variables
from worker.guest_identity import GuestIdentityError, ensure as ensure_guest_identity, required as guest_identity_required
from worker.heartbeat import heartbeat as update_heartbeat
from worker.netbox import NetBoxAllocation, NetBoxClient, NetBoxError, configured as netbox_configured
from worker.preboot import (
    assert_stopped_plan,
    expectation as preboot_expectation,
    load_expectation as load_preboot_expectation,
    mismatches as preboot_mismatches,
    save_expectation as save_preboot_expectation,
)
from worker.proxmox import ProxmoxClient, ProxmoxError, credentials, validate_endpoint
from worker.resilience import STOP, Backoff, identify_process, report, retry_until_ready


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


ACTIVE_WORK_OPERATION_STATUSES = (
    "running",
    "planning",
    "applying",
    "destroying",
    "powering",
)

DESTROY_PLACEHOLDER_PASSWORD = "IacpDestroyOnly!2026A"


def operation_output(value: object, secret: str | None = None) -> str:
    """Keep internal command output useful without allowing unbounded growth."""

    text = str(value or "")
    if secret:
        text = text.replace(secret, "<redacted>")
    limit = max(4096, int(os.getenv("IACP_OPERATION_OUTPUT_MAX_CHARS", "65536")))
    if len(text) <= limit:
        return text
    marker = "\n...[output truncated]...\n"
    available = max(0, limit - len(marker))
    head = available // 3
    tail = available - head
    return f"{text[:head]}{marker}{text[-tail:]}"


def stale_operation_seconds() -> float:
    return max(30.0, float(os.getenv("IACP_OPERATION_STALE_SECONDS", "900")))


def operation_lock_path(operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode()).hexdigest()
    return os.path.join(data_root(), "operation-locks", digest + ".lock")


@contextmanager
def operation_execution_lock(operation_id: str):
    """Serializa ejecución y permite que recovery detecte procesos vivos.

    Terraform hereda el descriptor de bloqueo. Si el worker queda pausado o
    termina mientras Terraform sigue activo, la reconciliación no puede tomar
    la titularidad hasta que también termine el proceso hijo.
    """
    lock_path = operation_lock_path(operation_id)
    os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    token = _OPERATION_LOCK_FD.set(descriptor)
    try:
        yield
    finally:
        _OPERATION_LOCK_FD.reset(token)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _try_operation_recovery_lock(operation_id: str) -> int | None:
    lock_path = operation_lock_path(operation_id)
    os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def _recovery_operation_exists(db, source_operation_id: str) -> bool:
    # Comparación exacta del campo JSON: un LIKE '%id%' recorría el texto de
    # cada payload y podía coincidir con el identificador en otro campo.
    return db.execute(
        """
        SELECT 1 FROM operations
        WHERE command='reconcile'
          AND payload_json::jsonb ->> 'source_operation_id' = ?
        LIMIT 1
        """,
        (source_operation_id,),
    ).fetchone() is not None


def recover_stale_operations() -> int:
    """Turn abandoned worker claims into auditable reconciliation work.

    A worker crash cannot safely replay a mutation. The source operation is
    closed as uncertain and a single reconcile operation is queued so the
    next healthy worker verifies the physical state before settling it.
    """

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_operation_seconds())).isoformat()
    recovered = 0
    with ExitStack() as lock_stack, database() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            f"""
            SELECT * FROM operations
            WHERE status IN ({','.join('?' for _ in ACTIVE_WORK_OPERATION_STATUSES)})
              AND (last_heartbeat IS NULL OR last_heartbeat < ?)
            ORDER BY created_at
            """,
            (*ACTIVE_WORK_OPERATION_STATUSES, cutoff),
        ).fetchall()
        for row in rows:
            operation = dict(row)
            operation_id = operation["operation_id"]
            recovery_lock = _try_operation_recovery_lock(operation_id)
            if recovery_lock is None:
                # El worker o Terraform aún mantiene la operación. El timeout
                # de heartbeat no basta para autorizar otra mutación física.
                continue
            lock_stack.callback(os.close, recovery_lock)
            lock_stack.callback(fcntl.flock, recovery_lock, fcntl.LOCK_UN)
            command = operation.get("command") or "unknown"
            timestamp = now()
            instance_id = operation.get("instance_id")
            pending_ipam = command == "plan" and instance_id and db.execute(
                """SELECT 1 FROM ip_allocations WHERE instance_id=?
                AND status IN ('allocating','allocated','release_pending') LIMIT 1""",
                (instance_id,),
            ).fetchone()
            terminal_status = "failed" if command == "plan" and not pending_ipam else "uncertain"
            detail = "El worker dejó de enviar heartbeat; se requiere reconciliación física."
            result = db.execute(
                """
                UPDATE operations
                SET status=?, phase=?, finished_at=?, last_heartbeat=?, exit_code=?, error=?
                WHERE operation_id=? AND status IN ('running', 'planning', 'applying', 'destroying', 'powering')
                  AND (last_heartbeat IS NULL OR last_heartbeat < ?)
                """,
                (
                    terminal_status,
                    "recovery_required" if terminal_status == "uncertain" else "worker_lost",
                    timestamp,
                    timestamp,
                    1,
                    "WORKER_HEARTBEAT_STALE",
                    operation_id,
                    cutoff,
                ),
            )
            if result.rowcount != 1:
                continue
            if instance_id:
                db.execute(
                    """
                    UPDATE instances
                    SET status=?, power_state=CASE WHEN ?='power' THEN 'unknown' ELSE power_state END, updated_at=?
                    WHERE instance_id=? AND status NOT IN ('deleted', 'failed')
                    """,
                    (terminal_status, command, timestamp, instance_id),
                )
            db.execute(
                """
                INSERT INTO events(operation_id, kind, status, occurred_at, detail)
                VALUES (?, 'recovery.required', ?, ?, ?)
                """,
                (operation_id, terminal_status, timestamp, detail),
            )

            if terminal_status == "failed" and operation.get("vmid") is not None:
                db.execute(
                    """
                    UPDATE vmid_leases
                    SET status='released', released_at=?
                    WHERE vmid=? AND operation_id=? AND status='held'
                    """,
                    (timestamp, int(operation["vmid"]), operation_id),
                )

            if (
                terminal_status != "uncertain"
                or command == "reconcile"
                or not instance_id
                or _recovery_operation_exists(db, operation_id)
            ):
                recovered += 1
                continue

            payload = json.dumps(
                {
                    "command": "reconcile",
                    "action": "reconcile",
                    "instance_id": instance_id,
                    "source_operation_id": operation_id,
                    "stack": operation.get("stack") or "mng01",
                },
                sort_keys=True,
            )
            recovery_id = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO operations(
                  operation_id, command, action, status, payload_json, instance_id,
                  plan_id, stack, created_at, phase
                ) VALUES (?, 'reconcile', 'reconcile', 'pending', ?, ?, ?, ?, ?, 'recovery')
                """,
                (
                    recovery_id,
                    payload,
                    instance_id,
                    operation.get("plan_id"),
                    operation.get("stack") or "mng01",
                    timestamp,
                ),
            )
            db.execute(
                """
                INSERT INTO events(operation_id, kind, status, occurred_at, detail)
                VALUES (?, 'operation.created', 'pending', ?, ?)
                """,
                (recovery_id, timestamp, f"reconcile de {operation_id}"),
            )
            recovered += 1
        db.execute("COMMIT")
    return recovered


def close_resolved_reconcile_attempts() -> int:
    """Cierra intentos de reconcile uncertain cuyo origen ya quedó resuelto.

    Si un reintento posterior confirmó el resultado del origen (succeeded o
    failed), el intento anterior no tiene nada que reconciliar; quedarse en
    uncertain bloqueaba matrices y gates que exigen cero inciertas.
    """

    with database() as db:
        rows = db.execute(
            """
            SELECT r.operation_id, src.operation_id AS source_id, src.status AS source_status
            FROM operations r
            JOIN operations src
              ON src.operation_id = r.payload_json::jsonb ->> 'source_operation_id'
            WHERE r.command = 'reconcile' AND r.status = 'uncertain'
              AND src.status IN ('succeeded', 'failed')
            """
        ).fetchall()
    closed = 0
    for row in rows:
        detail = f"El origen {row['source_id']} ya terminó en {row['source_status']}; este intento quedó superado."
        with database() as db:
            result = db.execute(
                """
                UPDATE operations
                SET status='failed', phase='superseded', finished_at=?, last_heartbeat=?,
                    exit_code=1, output=?, error='RECONCILIATION_SUPERSEDED_BY_CONFIRMED_LIFECYCLE'
                WHERE operation_id=? AND status='uncertain'
                """,
                (now(), now(), detail, row["operation_id"]),
            )
            if result.rowcount:
                db.execute(
                    "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, 'operation.superseded_by_lifecycle', 'failed', ?, ?)",
                    (row["operation_id"], now(), detail),
                )
                closed += 1
    return closed


DETERMINISTIC_ERROR_MARKERS = (
    "ssh public key validation",
    "invalid public key",
    "ssh_public_key_required",
    "validation error",
    "invalid value",
    "must be provided",
    "must be a valid",
    "resource pool",
    "proxmox_pool_not_found",
    "proxmox_pool_required",
    "power_permission_denied",
    "power_identity_mismatch",
    "power_task_identity_mismatch",
    "vmid_range_exhausted",
    "ipam_not_configured",
    "ipam_configuration_invalid",
    "ipam_prefix_invalid",
    "ipam_prefix_pool_unsafe",
    "ipam_gateway_not_reserved",
    "ipam_pool_exhausted",
    "ipam_allocation_busy",
    "ipam_tag_not_found",
    "ipam_permission_denied",
    "ipam_identity_mismatch",
    "ipam_instance_required",
    "protected_instance",
    "network_policy_mismatch",
    "terraform_start_before_validation_blocked",
    "terraform_plan_inspection_failed",
    "terraform_unexpected_resource_change",
    "terraform_plan_identity_mismatch",
    "terraform_vm_change_not_found",
    "preboot_expectation_required",
    "preboot_validation_failed",
    "preboot_dns_vm_already_running",
    "preboot_expectation_mismatch",
)

RETRYABLE_COMMAND_MARKERS = (
    "temporary failure in name resolution",
    "server misbehaving",
    "no address associated with hostname",
    "could not resolve host",
    "connection reset by peer",
    "connection refused",
    "i/o timeout",
    "tls handshake timeout",
    "transport is closing",
    "can't lock file",
    "got timeout",
    "502 bad gateway",
    "503 service unavailable",
)


def deterministic_error(value: object) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in DETERMINISTIC_ERROR_MARKERS)


def retryable_command_error(value: object) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in RETRYABLE_COMMAND_MARKERS)


def claim(worker_id: str):
    with database() as db:
        max_concurrent = int(os.getenv("IACP_MAX_CONCURRENT", "6"))
        active = db.execute(
            """
            SELECT COUNT(*) AS count FROM operations
            WHERE status IN ('running', 'planning', 'applying', 'destroying', 'powering')
            """
        ).fetchone()["count"]
        if active >= max_concurrent:
            return None
        # Avoid taking SQLite's writer lock when the queue is empty. The
        # second read is repeated after BEGIN IMMEDIATE because another
        # worker may claim the operation between these two steps.
        pending = db.execute(
            "SELECT 1 FROM operations WHERE status='pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if pending is None:
            return None
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            """
            SELECT COUNT(*) AS count FROM operations
            WHERE status IN ('running', 'planning', 'applying', 'destroying', 'powering')
            """
        ).fetchone()["count"]
        if active >= max_concurrent:
            db.execute("COMMIT")
            return None
        row = db.execute(
            """
            SELECT pending.* FROM operations AS pending
            WHERE pending.status='pending' AND NOT EXISTS (
                SELECT 1 FROM operations AS active
                WHERE active.instance_id=pending.instance_id
                  AND active.status IN ('running','planning','applying','destroying','powering')
            )
            ORDER BY pending.created_at LIMIT 1
            """
        ).fetchone()
        if row is None:
            db.execute("COMMIT")
            return None
        started = now()
        db.execute(
            """
            UPDATE operations
            SET status='running', worker_id=?, started_at=?, last_heartbeat=?,
                phase='claimed', attempt_count=COALESCE(attempt_count, 0) + 1
            WHERE operation_id=?
            """,
            (worker_id, started, started, row["operation_id"]),
        )
        db.execute(
            "INSERT INTO events(operation_id, kind, status, occurred_at) VALUES (?, 'operation.started', 'running', ?)",
            (row["operation_id"], started),
        )
        db.execute("COMMIT")
        return dict(row) | {"worker_id": worker_id}


def emit(operation_id: str, kind: str, status: str, detail: str = "", *, connection=None) -> None:
    with (database() if connection is None else nullcontext(connection)) as db:
        db.execute(
            "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, ?, ?, ?, ?)",
            (operation_id, kind, status, now(), detail),
        )


# Columnas que un worker puede actualizar. Los nombres se interpolan en el
# SQL, así que no se aceptan claves fuera de esta lista.
UPDATABLE_OPERATION_COLUMNS = frozenset({
    "command", "action", "status", "payload_json", "plan_id", "stack",
    "commercial_context_json", "environment", "instance_id", "vmid",
    "target_node", "secret_path", "workspace_path", "plan_path",
    "plan_operation_id", "worker_id", "started_at", "finished_at",
    "last_heartbeat", "phase", "attempt_count", "exit_code", "output", "error",
})


class OperationFencedError(RuntimeError):
    """El worker perdió la propiedad de su operación (heartbeat obsoleto).

    ``recover_stale_operations`` ya la cerró como ``uncertain`` y encoló una
    reconciliación; las escrituras tardías del worker original no deben
    sobrescribir ese resultado ni el estado de la instancia.
    """


# Operación que ejecuta este proceso: (worker_id, operation_id).
_FENCE: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("iacp_fence", default=None)
_OPERATION_LOCK_FD: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "iacp_operation_lock_fd", default=None
)
_FENCED_SQL = (
    "EXISTS (SELECT 1 FROM events WHERE events.operation_id = ? AND events.kind = 'recovery.required')"
)


def heartbeat(
    worker_id: str,
    status: str,
    operation_id: str | None = None,
    *,
    phase: str | None = None,
) -> None:
    if not update_heartbeat(
        worker_id,
        status,
        operation_id,
        phase=phase,
        require_owned=_FENCE.get() is not None and operation_id is not None,
    ):
        raise OperationFencedError(f"la operación {operation_id} ya no pertenece al worker")


def _fenced_operation_for(operation_id: str | None) -> str | None:
    fence = _FENCE.get()
    if fence is None or operation_id is None or fence[1] != operation_id:
        return None
    return fence[1]


def _raise_fenced(db, operation_id: str) -> None:
    worker_id = (_FENCE.get() or ("", ""))[0]
    db.execute(
        "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, 'operation.fenced', 'uncertain', ?, ?)",
        (operation_id, now(), f"Escritura tardía de {worker_id} rechazada; la operación ya fue recuperada."),
    )
    raise OperationFencedError(f"operación {operation_id} recuperada por heartbeat obsoleto")


def ensure_operation_owned(db, operation_id: str | None) -> None:
    fenced = _fenced_operation_for(operation_id)
    if fenced and db.execute(f"SELECT {_FENCED_SQL} AS fenced", (fenced,)).fetchone()["fenced"]:
        _raise_fenced(db, fenced)


def assert_active_operation_owned(db, worker_id: str, operation_id: str) -> None:
    statuses = ",".join("?" for _ in ACTIVE_WORK_OPERATION_STATUSES)
    row = db.execute(
        f"""SELECT 1 FROM operations WHERE operation_id=? AND worker_id=?
        AND status IN ({statuses}) AND NOT {_FENCED_SQL}""",
        (operation_id, worker_id, *ACTIVE_WORK_OPERATION_STATUSES, operation_id),
    ).fetchone()
    if row is None:
        raise OperationFencedError(f"la operación {operation_id} ya no pertenece al worker")


def update_operation(operation_id: str, *, connection=None, **values) -> None:
    if not values:
        return
    unknown = set(values) - UPDATABLE_OPERATION_COLUMNS
    if unknown:
        raise ValueError(f"columnas de operations no permitidas: {sorted(unknown)}")
    assignments = ", ".join(f"{name}=?" for name in values)
    with (database() if connection is None else nullcontext(connection)) as db:
        if _fenced_operation_for(operation_id):
            # Compare-and-set: la escritura solo aplica si la recuperación
            # automática no tomó la operación; se evalúa en la misma sentencia.
            worker_id, _ = _FENCE.get()
            statuses = ",".join("?" for _ in ACTIVE_WORK_OPERATION_STATUSES)
            result = db.execute(
                f"""UPDATE operations SET {assignments}
                WHERE operation_id=? AND worker_id=? AND status IN ({statuses})
                  AND NOT {_FENCED_SQL}""",
                (*values.values(), operation_id, worker_id, *ACTIVE_WORK_OPERATION_STATUSES, operation_id),
            )
            if result.rowcount != 1:
                _raise_fenced(db, operation_id)
            return
        db.execute(
            f"UPDATE operations SET {assignments} WHERE operation_id=?",
            (*values.values(), operation_id),
        )


def update_instance(
    instance_id: str | None,
    status: str,
    *,
    vmid: int | None = None,
    power_state: str | None = None,
    node: str | None = None,
    connection=None,
) -> None:
    if not instance_id:
        return
    with (database() if connection is None else nullcontext(connection)) as db:
        fence = _FENCE.get()
        owns_transaction = fence is not None and connection is None
        if owns_transaction:
            db.execute("BEGIN IMMEDIATE")
        if fence is not None:
            try:
                ensure_operation_owned(db, fence[1])
            except OperationFencedError:
                if owns_transaction:
                    # El evento de rechazo es la única escritura en esta transacción.
                    db.execute("COMMIT")
                raise
        if vmid is None and node is None:
            db.execute(
                "UPDATE instances SET status=?, updated_at=? WHERE instance_id=?",
                (status, now(), instance_id),
            )
        else:
            db.execute(
                """
                UPDATE instances
                SET status=?, vmid=COALESCE(?, vmid), node=COALESCE(?, node), updated_at=?
                WHERE instance_id=?
                """,
                (status, vmid, node, now(), instance_id),
            )
        if power_state is not None:
            db.execute(
                "UPDATE instances SET power_state=?, updated_at=? WHERE instance_id=?",
                (power_state, now(), instance_id),
            )
        if owns_transaction:
            db.execute("COMMIT")


def _update_instance_spec(instance_id: str | None, spec: dict) -> None:
    if not instance_id:
        return
    with database() as db:
        db.execute(
            "UPDATE instances SET spec_json=?, updated_at=? WHERE instance_id=?",
            (json.dumps(spec, sort_keys=True), now(), instance_id),
        )


def _update_operation_payload(operation_id: str, payload: dict) -> None:
    update_operation(operation_id, payload_json=json.dumps(payload, sort_keys=True))


def _ip_allocation(instance_id: str | None, *, active_only: bool = True) -> dict | None:
    if not instance_id:
        return None
    statuses = "AND status IN ('allocating', 'allocated', 'release_pending')" if active_only else ""
    with database() as db:
        row = db.execute(
            f"""
            SELECT * FROM ip_allocations
            WHERE instance_id=? {statuses}
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
    return dict(row) if row else None


def _begin_ip_allocation(
    *,
    instance_id: str,
    operation_id: str,
    marker: str,
) -> dict:
    allocation_id = str(uuid.uuid4())
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """
            SELECT * FROM ip_allocations
            WHERE instance_id=? AND profile='prod_static'
              AND status IN ('allocating', 'allocated', 'release_pending')
            ORDER BY created_at DESC LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
        if row is not None:
            if row["marker"] != marker:
                db.execute("ROLLBACK")
                raise NetBoxError("IPAM_ALLOCATION_CONFLICT")
            db.execute("COMMIT")
            return dict(row)
        created = now()
        db.execute(
            """
            INSERT INTO ip_allocations(
              allocation_id, instance_id, operation_id, provider, profile,
              marker, status, created_at
            ) VALUES (?, ?, ?, 'netbox', 'prod_static', ?, 'allocating', ?)
            """,
            (allocation_id, instance_id, operation_id, marker, created),
        )
        db.execute("COMMIT")
    return {
        "allocation_id": allocation_id,
        "instance_id": instance_id,
        "operation_id": operation_id,
        "marker": marker,
        "status": "allocating",
    }


def _complete_ip_allocation(
    allocation_id: str,
    allocation: NetBoxAllocation,
    *,
    operation_id: str | None = None,
    instance_id: str | None = None,
) -> None:
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            fence = _FENCE.get()
            if fence is not None:
                if operation_id != fence[1]:
                    raise OperationFencedError("IPAM_ALLOCATING_OPERATION_MISMATCH")
                assert_active_operation_owned(db, *fence)
            result = db.execute(
                """
                UPDATE ip_allocations
                SET netbox_ip_id=?, address=?, gateway=?, status='allocated', last_error=''
                WHERE allocation_id=? AND status='allocating'
                  AND (CAST(? AS TEXT) IS NULL OR operation_id=?)
                  AND (CAST(? AS TEXT) IS NULL OR instance_id=?)
                """,
                (
                    allocation.ip_id,
                    allocation.address,
                    allocation.gateway,
                    allocation_id,
                    operation_id,
                    operation_id,
                    instance_id,
                    instance_id,
                ),
            )
            if result.rowcount != 1:
                raise NetBoxError("IPAM_ALLOCATION_OWNERSHIP_UNCONFIRMED", uncertain=True)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise


def _mark_ip_allocation(
    allocation_id: str,
    status: str,
    *,
    operation_id: str | None = None,
    instance_id: str | None = None,
    expected_statuses: tuple[str, ...] = ("allocating", "allocated", "release_pending"),
    error: str = "",
) -> None:
    released_at = now() if status == "released" else None
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            fence = _FENCE.get()
            if fence is not None:
                if operation_id != fence[1]:
                    raise OperationFencedError("IPAM_ALLOCATION_OPERATION_MISMATCH")
                assert_active_operation_owned(db, *fence)
            if not expected_statuses:
                raise ValueError("IPAM_EXPECTED_STATUS_REQUIRED")
            statuses = ",".join("?" for _ in expected_statuses)
            result = db.execute(
                """
                UPDATE ip_allocations
                SET status=?, released_at=COALESCE(?, released_at), last_error=?
                WHERE allocation_id=? AND status IN (""" + statuses + ")"
                + (" AND instance_id=?" if instance_id is not None else ""),
                (status, released_at, error[:256], allocation_id, *expected_statuses)
                + ((instance_id,) if instance_id is not None else ()),
            )
            if result.rowcount != 1:
                raise NetBoxError("IPAM_ALLOCATION_OWNERSHIP_UNCONFIRMED", uncertain=True)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise


def _ensure_static_allocation(
    *,
    worker_id: str,
    operation_id: str,
    instance_id: str | None,
    vmid: int,
    payload: dict,
    source_operation: dict,
) -> dict:
    spec = dict(payload.get("spec") or {})
    profile = spec.get("network_profile") or (
        "prod_static" if spec.get("network_mode") == "static" else "lab_dhcp"
    )
    if profile != "prod_static":
        return spec
    if (os.getenv("IACP_PROD_STATIC_ENABLED", "0") or "").strip().lower() in {
        "", "0", "false", "no", "off"
    }:
        raise NetBoxError("IPAM_NOT_CONFIGURED")
    if not instance_id:
        raise NetBoxError("IPAM_INSTANCE_REQUIRED")
    if not netbox_configured():
        raise NetBoxError("IPAM_NOT_CONFIGURED")
    context = json.loads(source_operation.get("commercial_context_json") or "{}")
    marker = f"iacp-instance={instance_id}"
    client = NetBoxClient()
    setter = getattr(client, "set_heartbeat", None)
    if setter is not None:
        setter(lambda: heartbeat(worker_id, "running", operation_id, phase="ipam_allocate"))
    allocation = _ip_allocation(instance_id)
    new_intent = allocation is None
    if allocation is None:
        allocation = _begin_ip_allocation(
            instance_id=instance_id,
            operation_id=operation_id,
            marker=marker,
        )
    if allocation.get("status") == "release_pending":
        raise NetBoxError("IPAM_RELEASE_PENDING")
    if allocation.get("status") != "allocated" or not allocation.get("netbox_ip_id"):
        try:
            reserved = client.allocate(
                instance_id=instance_id,
                operation_id=operation_id,
                vmid=vmid,
                name=str(spec.get("name") or instance_id),
                customer_tag=context.get("customer_tag"),
                tags=spec.get("tags", []),
            )
            _complete_ip_allocation(
                allocation["allocation_id"],
                reserved,
                operation_id=operation_id,
                instance_id=instance_id,
            )
            allocation.update(
                netbox_ip_id=reserved.ip_id,
                address=reserved.address,
                gateway=reserved.gateway,
                status="allocated",
            )
            emit(operation_id, "ipam.address.reserved", "succeeded", reserved.address)
        except NetBoxError as error:
            if error.code == "IPAM_ALLOCATION_BUSY" and not new_intent:
                raise NetBoxError("IPAM_REQUEST_UNCERTAIN", uncertain=True) from error
            _mark_ip_allocation(
                allocation["allocation_id"],
                "release_pending" if error.uncertain else "released",
                operation_id=operation_id,
                instance_id=instance_id,
                expected_statuses=("allocating",),
                error=error.code,
            )
            raise
    spec.update(
        {
            "network_profile": "prod_static",
            "network_mode": "static",
            "vlan_id": None,
            "dns_servers": ["1.1.1.1", "8.8.8.8"],
            "ipv4_cidr": allocation["address"],
            "ipv4_gateway": allocation["gateway"],
        }
    )
    payload["spec"] = spec
    _update_operation_payload(operation_id, payload)
    _update_instance_spec(instance_id, spec)
    return spec


def _release_static_allocation(instance_id: str | None, operation_id: str) -> bool:
    allocation = _ip_allocation(instance_id)
    if allocation is None:
        return True
    fence = _FENCE.get()
    execution_operation_id = fence[1] if fence is not None else operation_id
    try:
        instance = instance_identity(instance_id)
        if instance.get("vmid") is not None:
            if not physical_presence(ProxmoxClient(), int(instance["vmid"]), False):
                raise NetBoxError("IPAM_VM_STILL_PRESENT", uncertain=True)
        client = NetBoxClient()
        fence = _FENCE.get()
        setter = getattr(client, "set_heartbeat", None)
        if setter is not None and fence is not None:
            worker_id, active_id = fence
            setter(lambda: heartbeat(worker_id, "running", active_id, phase="ipam_release"))
        client.release(allocation, marker=allocation["marker"])
    except (NetBoxError, ProxmoxError) as error:
        _mark_ip_allocation(
            allocation["allocation_id"],
            "release_pending",
            operation_id=execution_operation_id,
            instance_id=instance_id,
            expected_statuses=("allocating", "allocated", "release_pending"),
            error=getattr(error, "code", "IPAM_ABSENCE_UNCONFIRMED"),
        )
        emit(operation_id, "ipam.release.pending", "uncertain", "IPAM_RELEASE_PENDING")
        return False
    _mark_ip_allocation(
        allocation["allocation_id"],
        "released",
        operation_id=execution_operation_id,
        instance_id=instance_id,
        expected_statuses=("allocating", "allocated", "release_pending"),
    )
    emit(operation_id, "ipam.address.released", "succeeded")
    return True


def find_operation(operation_id: str) -> dict | None:
    with database() as db:
        row = db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
    return dict(row) if row else None


def instance_plan_operation(instance_id: str) -> str | None:
    with database() as db:
        row = db.execute(
            "SELECT operation_id FROM instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
    return row["operation_id"] if row else None


def instance_password_was_provisioned(instance_id: str | None) -> bool:
    """Read the original operation's non-secret credential-presence metadata."""

    source_id = instance_plan_operation(instance_id or "")
    source = find_operation(source_id) if source_id else None
    if source is None or source.get("command") not in {"plan", "provision"}:
        raise ProxmoxError("PREBOOT_EXPECTATION_REQUIRED")
    return bool(source.get("secret_path"))


def data_root() -> str:
    return os.getenv("IACP_WORKDATA_DIR", "/var/lib/iacp-controlplane")


def source_stack_directory(stack: str) -> str:
    if stack == "local":
        return os.getenv("IACP_LOCAL_TERRAFORM_DIR", "fixtures/local")
    return os.getenv("IACP_TERRAFORM_DIR", "environments/mng01")


def select_target_node(client: ProxmoxClient) -> str:
    """Choose an online MNG node with the fewest VMs, preferring mng01."""

    configured = cluster_nodes()
    node_rows = client.nodes()
    online = {
        str(row.get("node"))
        for row in node_rows
        if row.get("node") in configured and row.get("status", "online") == "online"
    }
    storage_id = load_catalog().get("storage")
    candidates = []
    for node in configured:
        if node not in online:
            continue
        try:
            storage = client.storage_status(node, storage_id) if storage_id else {}
        except ProxmoxError:
            continue
        if storage and storage.get("active") in {0, False}:
            continue
        candidates.append(node)
    if not candidates:
        raise RuntimeError("MNG_NODE_UNAVAILABLE")

    counts = {node: 0 for node in candidates}
    for item in client.inventory():
        node = item.get("node")
        if node in counts:
            counts[node] += 1
    return min(candidates, key=lambda node: (counts[node], configured.index(node)))


def create_workspace(operation_id: str, source_path: str, stack: str) -> tuple[str, str]:
    root = os.path.join(data_root(), "plans", operation_id, "terraform")
    if os.path.exists(root):
        raise RuntimeError(f"el workspace ya existe: {root}")
    ignore = shutil.ignore_patterns(
        ".terraform",
        "*.tfstate",
        "*.tfstate.*",
        "generated.auto.tfvars.json",
        "plan.tfplan",
    )
    shutil.copytree(source_path, root, ignore=ignore)
    workspace = os.path.join(root, source_stack_directory(stack))
    if not os.path.isdir(workspace):
        raise RuntimeError(f"no existe el stack Terraform {workspace}")
    # The image build initializes the certified provider once. Reuse that
    # immutable provider directory instead of downloading it for every
    # operation or copying tens of megabytes into each workspace.
    source_providers = os.path.join(
        source_path,
        source_stack_directory(stack),
        ".terraform",
        "providers",
    )
    target_providers = os.path.join(workspace, ".terraform", "providers")
    if os.path.isdir(source_providers) and not os.path.exists(target_providers):
        os.makedirs(os.path.dirname(target_providers), exist_ok=True)
        os.symlink(source_providers, target_providers)
    return root, workspace


def write_variables(workspace: str, values: dict) -> str:
    path = os.path.join(workspace, "generated.auto.tfvars.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def local_variables(spec: dict) -> dict:
    return {
        "environment": "local",
        "instance": {
            "name": spec["name"],
            "image": spec["image"],
            "vcpus": spec["vcpus"],
            "memory_mb": spec["memory_mb"],
            "disk_gb": spec["disk_gb"],
            "network_mode": spec.get("network_mode", "dhcp"),
            "started": spec.get("started", True),
            "tags": spec.get("tags", []),
        },
    }


# Variables que Terraform no necesita y que no debe heredar: credenciales de
# PostgreSQL, del API de IACP, de NetBox, de Wasabi y tokens duplicados. El
# provider de Proxmox recibe su token por PROXMOX_VE_API_TOKEN.
_TERRAFORM_ENV_ALLOW = frozenset({"PROXMOX_VE_API_TOKEN", "PROXMOX_VE_ENDPOINT"})
_TERRAFORM_ENV_DENY_PREFIXES = ("PG", "AWS_", "WASABI_", "NETBOX_", "IACP_NETBOX", "IACP_WASABI")
_TERRAFORM_ENV_DENY_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "ACCESS_KEY", "DATABASE_URL")


_PROXMOX_CREDENTIALS: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "iacp_proxmox_credentials", default=None
)


def load_environment() -> None:
    endpoint, token = credentials()
    validate_endpoint(endpoint)
    _PROXMOX_CREDENTIALS.set((endpoint, token))


def terraform_environment() -> dict[str, str]:
    environment = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in _TERRAFORM_ENV_ALLOW or upper.startswith("TF_"):
            environment[name] = value
            continue
        if upper.startswith(_TERRAFORM_ENV_DENY_PREFIXES):
            continue
        if any(marker in upper for marker in _TERRAFORM_ENV_DENY_MARKERS):
            continue
        environment[name] = value
    if proxmox_credentials := _PROXMOX_CREDENTIALS.get():
        endpoint, token = proxmox_credentials
        environment.update(PROXMOX_VE_ENDPOINT=endpoint, PROXMOX_VE_API_TOKEN=token)
    return environment


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Detiene Terraform y sus providers; ``kill()`` dejaba providers huérfanos."""

    grace = max(1.0, float(os.getenv("IACP_TERRAFORM_KILL_GRACE_SECONDS", "20")))
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # El líder puede salir antes que sus providers. El grupo sigue siendo
    # alcanzable por su PGID aunque el proceso Terraform haya terminado.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def read_terraform_output(handle, secret: str | None = None) -> str:
    size = os.fstat(handle.fileno()).st_size
    limit = max(4096, int(os.getenv("IACP_OPERATION_OUTPUT_MAX_CHARS", "65536"))) * 4
    secret_bytes = secret.encode() if secret else b""

    def redact(data: bytes) -> bytes:
        return data.replace(secret_bytes, b"<redacted>") if secret_bytes else data

    handle.seek(0)
    if size <= limit:
        data = redact(handle.read(limit))
    else:
        head_limit = limit // 3
        tail_limit = limit - head_limit
        overlap = len(secret_bytes)
        head = redact(handle.read(head_limit + overlap))[:head_limit]
        handle.seek(max(0, size - tail_limit - overlap))
        tail = redact(handle.read(tail_limit + overlap))[-tail_limit:]
        data = head + b"\n...[output truncated]...\n" + tail
    return data.decode("utf-8", errors="replace")


def run_commands(
    worker_id: str,
    operation_id: str,
    workspace: str,
    commands: list[list[str]],
    *,
    secret_path: str | None = None,
    phase: str | None = None,
) -> tuple[int, str, str]:
    deadline = time.monotonic() + int(os.getenv("IACP_TERRAFORM_TIMEOUT", "1800"))
    output: list[str] = []
    errors: list[str] = []
    max_log_bytes = max(1024 * 1024, int(os.getenv("IACP_TERRAFORM_OUTPUT_MAX_BYTES", str(16 * 1024 * 1024))))
    environment = terraform_environment()
    environment.pop("TF_VAR_root_password", None)
    external_password = workspace_password_by_worker(workspace)
    plugin_cache = os.getenv("IACP_TERRAFORM_PLUGIN_CACHE_DIR", "").strip()
    if plugin_cache:
        environment["TF_PLUGIN_CACHE_DIR"] = plugin_cache
    password = None if external_password else read_secret(secret_path)
    if password is not None:
        environment["TF_VAR_root_password"] = password
    elif not external_password and any(
        len(args) > 1 and args[0] == "terraform" and args[1] == "destroy" for args in commands
    ):
        # Terraform evaluates the module's access validation even during
        # destroy. The original bootstrap secret is intentionally deleted
        # after provisioning, so provide a destroy-only value that is never
        # persisted or applied to a guest. The existing resource identity and
        # state are what determine the objects removed by this command.
        environment["TF_VAR_root_password"] = DESTROY_PLACEHOLDER_PASSWORD
    for args in commands:
        command_attempt = 0
        max_attempts = max(1, int(os.getenv("IACP_TERRAFORM_RETRY_ATTEMPTS", "4")))
        while True:
            # La salida se vuelca a archivos temporales y no a PIPE: el bucle
            # de supervisión no lee las tuberías mientras el proceso corre, y
            # una salida mayor que el búfer del pipe (~64 KiB) bloquearía a
            # Terraform hasta vencer el timeout, incluso durante un apply.
            owns_operation = _FENCE.get() is not None
            if owns_operation:
                heartbeat(worker_id, "running", operation_id, phase=phase)
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                process = subprocess.Popen(
                    args,
                    cwd=workspace,
                    text=True,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=environment,
                    # Grupo de procesos propio: el timeout detiene también
                    # los providers que Terraform lanza como hijos.
                    start_new_session=True,
                    pass_fds=(lock_fd,) if (lock_fd := _OPERATION_LOCK_FD.get()) is not None else (),
                )
                heartbeat_every = max(1.0, float(os.getenv("IACP_COMMAND_HEARTBEAT_SECONDS", "5")))
                last_confirmed_heartbeat = time.monotonic()
                last_heartbeat = last_confirmed_heartbeat if owns_operation else 0.0
                heartbeat_grace = min(30.0, stale_operation_seconds() / 2)
                if owns_operation:
                    heartbeat_every = min(heartbeat_every, heartbeat_grace / 2)
                while process.poll() is None:
                    if any(os.fstat(log.fileno()).st_size > max_log_bytes for log in (stdout_file, stderr_file)):
                        _terminate_process_group(process)
                        raise RuntimeError("TERRAFORM_OUTPUT_LIMIT_EXCEEDED")
                    if time.monotonic() > deadline:
                        _terminate_process_group(process)
                        raise TimeoutError("Terraform excedió el tiempo configurado")
                    # Un heartbeat por segundo y worker eran ~6 escrituras/s
                    # con seis applies en paralelo; cada 5 s basta frente al
                    # umbral de 900 s de operación obsoleta.
                    if time.monotonic() - last_heartbeat >= heartbeat_every:
                        try:
                            heartbeat(worker_id, "running", operation_id, phase=phase)
                            last_confirmed_heartbeat = time.monotonic()
                            last_heartbeat = last_confirmed_heartbeat
                        except OperationFencedError:
                            _terminate_process_group(process)
                            raise
                        except Exception as error:  # noqa: BLE001
                            last_heartbeat = time.monotonic()
                            if last_heartbeat - last_confirmed_heartbeat >= heartbeat_grace:
                                _terminate_process_group(process)
                                raise RuntimeError("WORKER_HEARTBEAT_UNCONFIRMED") from error
                            report(worker_id, "heartbeat", error, heartbeat_every)
                    time.sleep(1)
                stdout = read_terraform_output(stdout_file, password)
                stderr = read_terraform_output(stderr_file, password)
            output.append(operation_output(stdout, password))
            errors.append(operation_output(stderr, password))
            if process.returncode == 0:
                break
            text = f"{stdout}\n{stderr}".lower()
            # Only initialization and planning are retried here. Replaying
            # apply/destroy after a lost response can duplicate a mutation;
            # execute() handles those commands through physical reconciliation.
            safe_to_retry = len(args) > 1 and args[1] in {"init", "plan"}
            if command_attempt + 1 >= max_attempts or not safe_to_retry or not retryable_command_error(text):
                return process.returncode, operation_output("".join(output), password), operation_output("".join(errors), password)
            command_attempt += 1
            heartbeat(worker_id, "running", operation_id, phase=phase)
            time.sleep(min(15.0, 2 * (2**(command_attempt - 1))))
    return 0, operation_output("".join(output), password), operation_output("".join(errors), password)


def terraform_commands(command: str, plan_path: str) -> list[list[str]]:
    init = [
        "terraform",
        "init",
        "-input=false",
        "-no-color",
        "-backend=false",
        "-lockfile=readonly",
    ]
    if command == "plan":
        return [
            init,
            ["terraform", "plan", "-input=false", "-no-color", "-out", plan_path],
        ]
    if command == "apply":
        return [init, ["terraform", "apply", "-input=false", "-no-color", plan_path]]
    if command == "destroy":
        return [
            init,
            ["terraform", "destroy", "-input=false", "-auto-approve", "-no-color"],
        ]
    if command == "provision":
        return [
            init,
            ["terraform", "plan", "-input=false", "-no-color", "-out", plan_path],
            ["terraform", "apply", "-input=false", "-no-color", plan_path],
        ]
    raise RuntimeError(f"orden Terraform no soportada: {command}")


def secret_path_for_command(command: str, source_operation: dict, operation: dict) -> str | None:
    """Return a bootstrap secret only for commands that need Terraform variables.

    Destroy never needs the root password. The provision worker deletes that
    one-shot secret after apply, so reusing its path during destroy would turn
    a valid cleanup into an uncertain operation when the file is gone.
    """

    if command == "destroy":
        return None
    return source_operation.get("secret_path") or operation.get("secret_path")


def physical_record(
    client: ProxmoxClient,
    vmid: int,
    *,
    heartbeat_callback=None,
    deadline: float | None = None,
) -> dict | None:
    """Read a VM record with a bounded retry window for transient transport errors."""

    attempts = 1 if deadline is not None else max(1, int(os.getenv("IACP_RECONCILE_ATTEMPTS", "6")))
    for attempt in range(attempts):
        if heartbeat_callback is not None:
            heartbeat_callback()
        try:
            if deadline is None:
                inventory = client.inventory(uncertain=True)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProxmoxError("PROXMOX_RECONCILE_TIMEOUT", uncertain=True)
                request_timeout = min(
                    remaining,
                    max(1.0, float(os.getenv("IACP_PROXMOX_REQUEST_TIMEOUT", "20"))),
                )
                inventory = client.inventory(
                    uncertain=True, timeout=request_timeout, attempts=1
                )
            return next(
                (
                    dict(item)
                    for item in inventory
                    if int(item.get("vmid", -1)) == int(vmid)
                ),
                None,
            )
        except ProxmoxError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(8.0, 0.5 * (2**attempt)))
    return None


def assert_lease_owner(vmid: int, owner: str | None) -> None:
    with database() as db:
        lease = db.execute(
            "SELECT operation_id, status FROM vmid_leases WHERE vmid=?",
            (vmid,),
        ).fetchone()
    if not owner or lease is None or lease["operation_id"] != owner or lease["status"] != "held":
        raise ProxmoxError("VMID_LEASE_OWNER_MISMATCH", uncertain=True)


def assert_physical_identity(client: ProxmoxClient, vmid: int, node: str, name: str, owner: str) -> None:
    assert_lease_owner(vmid, owner)
    source = find_operation(owner)
    marker = json.loads(source["payload_json"]).get("physical_identity_tag") if source else None
    if marker:
        config = client.vm_config(node, vmid)
        tags = str(config.get("tags") or "").split(";")
        if config.get("name") != name or config.get("template") or marker not in tags:
            raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)


def require_free_provision_target(client: ProxmoxClient, vmid: int, owner: str) -> None:
    assert_lease_owner(vmid, owner)
    if physical_record(client, vmid) is not None:
        raise ProxmoxError("VMID_PHYSICAL_RESOURCE_ALREADY_PRESENT", uncertain=True)


def physical_presence(
    client: ProxmoxClient,
    vmid: int,
    expected: bool,
    *,
    name: str | None = None,
    node: str | None = None,
    heartbeat_callback=None,
    deadline: float | None = None,
) -> bool:
    record = physical_record(
        client, vmid, heartbeat_callback=heartbeat_callback, deadline=deadline
    )
    if not expected:
        return record is None
    if record is None:
        return False
    observed_name = record.get("name")
    observed_node = record.get("node")
    # Proxmox cluster resources can briefly return the previous VM record
    # while a VMID is being reused.  Treat an identity mismatch as a
    # non-converged observation so reconcile_physical() can sample again;
    # declaring uncertainty on the first stale read creates a false positive
    # immediately after a successful destroy/create pair.
    if name and observed_name != name:
        return False
    if node and observed_node != node:
        return False
    return not bool(record.get("template"))


def reconcile_physical(
    client: ProxmoxClient,
    vmid: int,
    expected: bool,
    *,
    name: str | None = None,
    node: str | None = None,
    heartbeat_callback=None,
) -> bool:
    """Confirm the expected physical presence before settling an operation."""

    deadline = time.monotonic() + max(
        5.0, float(os.getenv("IACP_RECONCILE_TIMEOUT", "180"))
    )
    while time.monotonic() < deadline:
        if heartbeat_callback is not None:
            heartbeat_callback()
        try:
            if physical_presence(
                client,
                vmid,
                expected,
                name=name,
                node=node,
                heartbeat_callback=heartbeat_callback,
                deadline=deadline,
            ):
                return True
        except ProxmoxError as error:
            transient = error.status is None or error.status in {408, 429} or error.status >= 500
            if not transient:
                raise
            if time.monotonic() >= deadline:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        backoff = max(0.1, float(os.getenv("IACP_RECONCILE_BACKOFF", "2")))
        time.sleep(min(8.0, backoff, remaining))
    return False


def terraform_state_tracks_vmid(workspace: str | None, vmid: int) -> bool | None:
    """Indica si el state local registra la VM; ``None`` si no se puede probar."""

    if not workspace:
        return None
    path = os.path.join(workspace, "terraform.tfstate")
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    resources = state.get("resources")
    if not isinstance(resources, list):
        return None
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("type") != "proxmox_virtual_environment_vm":
            continue
        for instance in resource.get("instances") or []:
            attributes = (instance or {}).get("attributes") or {}
            observed = attributes.get("vm_id")
            # Una instancia sin vm_id legible no permite descartar la VM.
            if observed is None or int(observed) == int(vmid):
                return True
    return False


def require_reconciled_terraform_state(workspace: str, vmid: int) -> None:
    if terraform_state_tracks_vmid(workspace, vmid) is not True:
        raise ProxmoxError("TERRAFORM_STATE_OWNERSHIP_UNCONFIRMED", uncertain=True)


def terraform_state_owns_vm(
    workspace: str,
    vmid: int,
    *,
    name: str,
    node: str,
    pool: str,
    owner_tag: str,
) -> bool:
    """Valida que el state de esta operación atribuya la VM a su titular."""

    try:
        with open(os.path.join(workspace, "terraform.tfstate"), encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return False
    resources = state.get("resources")
    if not isinstance(resources, list):
        return False
    managed = [
        resource
        for resource in resources
        if isinstance(resource, dict) and resource.get("mode", "managed") == "managed"
    ]
    # terraform destroy operates on every managed resource in this workspace.
    # The IACP KVM module owns exactly one Proxmox VM; reject an expanded or
    # corrupted state instead of validating only the expected VMID.
    if len(managed) != 1 or managed[0].get("type") != "proxmox_virtual_environment_vm":
        return False
    instances = managed[0].get("instances")
    if not isinstance(instances, list) or len(instances) != 1:
        return False
    attributes = (instances[0] or {}).get("attributes") or {}
    try:
        correct_vmid = int(attributes.get("vm_id")) == int(vmid)
    except (TypeError, ValueError):
        return False
    tags = attributes.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(";")
    return bool(
        correct_vmid
        and isinstance(tags, list)
        and attributes.get("name") == name
        and attributes.get("node_name") == node
        and attributes.get("pool_id") == pool
        and attributes.get("template") is False
        and owner_tag in tags
    )


def assert_destroy_physical_identity(
    client: ProxmoxClient,
    vmid: int,
    node: str,
    name: str,
    owner: str,
    workspace: str,
) -> None:
    """Permite destruir por Terraform un clon parcial con titularidad probada.

    La etiqueta física se aplica después de clonar. Si el worker se interrumpe
    antes de esa escritura, la combinación de state, lease, identidad y tarea
    `qmclone` atribuible permite únicamente la limpieza normal por Terraform.
    """

    source = find_operation(owner)
    try:
        source_payload = json.loads(source.get("payload_json") or "{}") if source else {}
    except (TypeError, ValueError):
        source_payload = {}
    marker = source_payload.get("physical_identity_tag")
    state_root = source.get("workspace_path") if source else None
    expected_workspace = (
        os.path.join(state_root, source_stack_directory(source.get("stack") or "mng01"))
        if state_root
        else None
    )
    pool = configured_pool_id()
    if (
        source is None
        or not marker
        or not expected_workspace
        or os.path.realpath(workspace) != os.path.realpath(expected_workspace)
        or not terraform_state_owns_vm(
            workspace,
            int(vmid),
            name=name,
            node=node,
            pool=pool,
            owner_tag=marker,
        )
    ):
        raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)

    try:
        assert_physical_identity(client, vmid, node, name, owner)
        return
    except ProxmoxError as error:
        if str(error) != "VM_PHYSICAL_OWNER_MISMATCH":
            raise

    identity = instance_identity(source.get("instance_id") if source else None)
    persisted_spec = instance_spec(source.get("instance_id") if source else None)
    if (
        source is None
        or source.get("command") != "provision"
        or source.get("status") != "uncertain"
        or source.get("stack") != "mng01"
        or not source.get("started_at")
        or not source.get("finished_at")
        or source.get("instance_id") != identity.get("instance_id")
        or identity.get("operation_id") != owner
        or identity.get("name") != name
        or int(identity.get("vmid") or -1) != int(vmid)
        or identity.get("node") != node
        or persisted_spec.get("name") != name
        or int(source.get("vmid") or -1) != int(vmid)
        or source.get("target_node") != node
    ):
        raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)
    if not int(os.getenv("IACP_VMID_MIN", "40000")) <= int(vmid) <= int(
        os.getenv("IACP_VMID_MAX", "49999")
    ) or is_protected_vmid(vmid):
        raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)

    try:
        payload = json.loads(source.get("payload_json") or "{}")
    except (TypeError, ValueError):
        raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)
    spec = payload.get("spec") or {}
    marker = payload.get("physical_identity_tag")
    if (
        not isinstance(spec, dict)
        or spec.get("name") != name
        or not spec.get("image")
        or persisted_spec.get("image") != spec.get("image")
        or marker != f"iacp-owned-{owner}"
    ):
        raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)

    state_root = source.get("workspace_path")
    expected_workspace = (
        os.path.join(state_root, source_stack_directory("mng01"))
        if state_root
        else None
    )
    if (
        not expected_workspace
        or os.path.realpath(workspace) != os.path.realpath(expected_workspace)
    ):
        raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)

    record = physical_record(client, int(vmid))
    if (
        record is None
        or record.get("name") != name
        or record.get("node") != node
        or record.get("pool") != pool
        or record.get("status") != "stopped"
        or bool(record.get("template"))
    ):
        raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)
    config = client.vm_config(node, int(vmid))
    tags = str(config.get("tags") or "").split(";")
    if (
        config.get("name") != name
        or bool(config.get("template"))
        or marker in tags
        or any(tag.startswith("iacp-owned-") for tag in tags)
    ):
        raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)

    from worker.golden import catalog as golden_catalog

    template = (golden_catalog().get(spec["image"]) or {}).get("template_vm_id")
    clone_proof = getattr(client, "completed_clone_task_during", None)
    if (
        not template
        or not callable(clone_proof)
        or not clone_proof(
            node,
            int(vmid),
            int(template),
            source["started_at"],
            source["finished_at"],
        )
    ):
        raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)
    emit(
        owner,
        "destroy.partial_clone_owner_verified",
        "ok",
        f"VMID {vmid}; titularidad probada por state y tarea qmclone.",
    )


def remove_orphan_vm(
    client: ProxmoxClient,
    operation_id: str,
    instance_id: str | None,
    vmid: int,
    workspace: str | None,
    *,
    heartbeat_callback=None,
) -> bool:
    """Elimina una VM que quedó fuera del state de Terraform.

    Si un provision falla entre la clonación y la configuración, la VM existe
    con el nombre de la instancia pero el state no la registra; ``terraform
    destroy`` no hace nada y cada reconciliación vuelve a ``uncertain`` sin
    converger. Solo se elimina cuando todas las guardas demuestran que la VM
    es de esta instancia; ante cualquier duda se conserva (fail-closed).
    """

    reasons: list[str] = []
    if terraform_state_tracks_vmid(workspace, vmid) is not False:
        reasons.append("state_no_demuestra_huerfana")
    minimum = int(os.getenv("IACP_VMID_MIN", "40001"))
    maximum = int(os.getenv("IACP_VMID_MAX", "49999"))
    if not minimum <= int(vmid) <= maximum or is_protected_vmid(vmid):
        reasons.append("vmid_fuera_de_rango_o_protegida")
    identity = instance_identity(instance_id)
    record = physical_record(client, vmid, heartbeat_callback=heartbeat_callback)
    if record is None:
        return True
    node = str(record.get("node") or "")
    try:
        pool = configured_pool_id()
    except ValueError:
        pool = ""
    if not identity.get("name") or record.get("name") != identity.get("name"):
        reasons.append("nombre_distinto")
    if not pool or record.get("pool") != pool:
        reasons.append("pool_distinto")
    if identity.get("node") and node != identity.get("node"):
        reasons.append("nodo_distinto")
    if record.get("template"):
        reasons.append("es_template")
    if record.get("status") != "stopped":
        reasons.append("vm_no_detenida")
    with database() as db:
        lease = db.execute(
            """
            SELECT o.operation_id FROM vmid_leases l JOIN operations o ON o.operation_id = l.operation_id
            WHERE l.vmid = ? AND o.instance_id = ? AND l.status='held'
            LIMIT 1
            """,
            (int(vmid), instance_id),
        ).fetchone()
    if lease is None:
        reasons.append("lease_de_otra_instancia")
    if reasons:
        emit(operation_id, "destroy.orphan.retained", "uncertain", ",".join(reasons))
        return False
    assert_physical_identity(client, int(vmid), node, identity["name"], lease["operation_id"])
    client.permissions(node, int(vmid), mutate=True)
    config = client.vm_config(node, int(vmid))
    if config.get("name") != identity.get("name") or config.get("template"):
        emit(operation_id, "destroy.orphan.retained", "uncertain", "config_distinta")
        return False
    emit(operation_id, "destroy.orphan.started", "running", f"VMID {vmid} fuera del state de Terraform")
    task = client._request(
        "DELETE",
        f"/nodes/{node}/qemu/{int(vmid)}?purge=1&destroy-unreferenced-disks=1",
        None,
        uncertain=True,
    )
    deadline = time.monotonic() + max(30.0, float(os.getenv("IACP_POWER_TIMEOUT", "180")))
    while isinstance(task, str) and task and time.monotonic() < deadline:
        if heartbeat_callback is not None:
            heartbeat_callback()
        status = client.get(f"/nodes/{node}/tasks/{task}/status", uncertain=True) or {}
        if status.get("status") == "stopped":
            if status.get("exitstatus") != "OK":
                emit(operation_id, "destroy.orphan.failed", "uncertain", str(status.get("exitstatus"))[:200])
                return False
            break
        time.sleep(2)
    removed = reconcile_physical(client, int(vmid), False, heartbeat_callback=heartbeat_callback)
    emit(
        operation_id,
        "destroy.orphan.completed" if removed else "destroy.orphan.failed",
        "succeeded" if removed else "uncertain",
        f"VMID {vmid}",
    )
    return removed


def instance_identity(instance_id: str | None) -> dict:
    if not instance_id:
        return {}
    with database() as db:
        row = db.execute(
            "SELECT instance_id, operation_id, vmid, node, name, status, power_state FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
    return dict(row) if row else {}


def instance_spec(instance_id: str | None) -> dict:
    if not instance_id:
        return {}
    with database() as db:
        row = db.execute(
            "SELECT spec_json FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row["spec_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def operation_spec(source_operation: dict, fallback_payload: dict | None = None) -> dict:
    # El payload en memoria contiene la reserva IPAM creada durante provision.
    # El snapshot source_operation todavía puede conservar el spec anterior.
    if isinstance(fallback_payload, dict) and isinstance(fallback_payload.get("spec"), dict):
        return fallback_payload["spec"]
    try:
        payload = json.loads(source_operation.get("payload_json") or "{}")
    except (TypeError, ValueError):
        payload = {}
    spec = payload.get("spec") if isinstance(payload, dict) else None
    if not isinstance(spec, dict) and isinstance(fallback_payload, dict):
        spec = fallback_payload.get("spec")
    return spec if isinstance(spec, dict) else {}


def expected_preboot_for_spec(
    spec: dict,
    *,
    vmid: int,
    node: str,
    client: ProxmoxClient,
    stack: str = "mng01",
    password_present: bool | None,
) -> dict:
    """Rebuild the expected portal/image contract for recovery and lifecycle starts."""

    if not isinstance(spec, dict) or not spec.get("image") or not spec.get("name"):
        raise ProxmoxError("PREBOOT_EXPECTATION_REQUIRED")
    golden = observe(spec["image"], client)
    values = golden_variables(
        spec,
        int(vmid),
        golden,
        stack,
        target_node=node,
        allowed_nodes=cluster_nodes(),
        # The original operation retains only whether a password was supplied;
        # the secret itself is removed after provisioning.
        has_root_password=(True if password_present is None else password_present),
        pool_id=configured_pool_id(),
    )
    return preboot_expectation(
        values["instance"],
        golden,
        password_present=password_present,
        node=node,
        vmid=int(vmid),
    )


def operation_preboot_expectation(
    workspace: str,
    spec: dict,
    *,
    vmid: int,
    node: str,
    client: ProxmoxClient,
    stack: str,
    password_present: bool | None,
) -> dict:
    """Use the plan-time contract; fall back only for pre-gate recovery records."""

    expectation_path = os.path.join(workspace, "preboot.expectation.json")
    if not os.path.exists(expectation_path):
        return expected_preboot_for_spec(
            spec,
            vmid=vmid,
            node=node,
            client=client,
            stack=stack,
            password_present=password_present,
        )
    expected = load_preboot_expectation(workspace)
    if (
        expected.get("vmid") != int(vmid)
        or expected.get("node") != node
        or expected.get("image_id") != spec.get("image")
        or expected.get("instance", {}).get("vm_name") != spec.get("name")
    ):
        raise ProxmoxError("PREBOOT_EXPECTATION_MISMATCH")
    return expected


def workspace_password_by_worker(workspace: str) -> bool:
    path = os.path.join(workspace, "preboot.expectation.json")
    return os.path.isfile(path) and bool(load_preboot_expectation(workspace).get("password_configured_by_worker"))


def configure_workspace_password(
    client: ProxmoxClient,
    workspace: str,
    *,
    proof_operation_id: str,
    node: str,
    vmid: int,
    name: str,
    expected: dict,
    secret_path: str | None,
) -> None:
    if not workspace_password_by_worker(workspace) or preboot_previously_verified(proof_operation_id):
        return
    password = read_secret(secret_path)
    if not password:
        raise ProxmoxError("CLOUDINIT_PASSWORD_SECRET_REQUIRED", uncertain=True)
    client.ensure_cloudinit_password(
        node, vmid, name,
        expected["instance"]["cloudinit_username"], password,
    )


def verify_preboot_configuration(
    operation_id: str,
    client: ProxmoxClient,
    *,
    node: str,
    vmid: int,
    name: str,
    expected: dict,
    require_stopped: bool = True,
    record: bool = True,
    proof_operation_id: str | None = None,
) -> dict:
    config = client.vm_config(node, int(vmid))
    power = client.power_state(node, int(vmid), name)
    power_state = power.get("power_state")
    failures = preboot_mismatches(
        config,
        expected,
        vmid=int(vmid),
        node=node,
        name=name,
        power_state=power_state,
        require_stopped=require_stopped,
    )
    if failures:
        detail = json.dumps(
            {"node": node, "vmid": int(vmid), "fields": failures},
            sort_keys=True,
        )
        if record:
            emit(operation_id, "kvm.preboot.rejected", "failed", detail)
        raise ProxmoxError("PREBOOT_VALIDATION_FAILED:" + ",".join(failures))
    if record:
        detail = json.dumps(
            {"node": node, "vmid": int(vmid), "power_state": power_state},
            sort_keys=True,
        )
        emit(operation_id, "kvm.preboot.verified", "succeeded", detail)
        if proof_operation_id and proof_operation_id != operation_id:
            emit(proof_operation_id, "kvm.preboot.verified", "succeeded", detail)
    return power


def ensure_guest_identity_for_operation(
    worker_id: str,
    operation_id: str,
    client: ProxmoxClient,
    *,
    stack: str,
    node: str,
    vmid: int,
    name: str,
    instance_id: str,
    spec: dict,
    started: bool = True,
) -> dict | None:
    if stack == "mng01" and spec.get("network_profile") == "prod_static":
        if not started:
            return None
        from worker.service_bootstrap import prepare
        try:
            result = prepare(client, node=node, vmid=int(vmid), name=name, instance_id=instance_id, spec=spec,
                             heartbeat=lambda: heartbeat(worker_id, "running", operation_id, phase="static_guest_validation"))
            if result:
                emit(operation_id, "guest.identity.verified", "succeeded", json.dumps({"ipv4": result.get("address"), "mode": "static-qga"}))
            return result
        except GuestIdentityError:
            raise
        except Exception as error:
            raise GuestIdentityError("STATIC_GUEST_VALIDATION_UNCERTAIN") from error
    if not guest_identity_required(spec, stack, vmid):
        if stack == "mng01" and started and spec.get("image") in {"ubuntu-2604", "almalinux-9", "almalinux-10"} and spec.get("network_profile", "lab_dhcp") == "lab_dhcp":
            from worker.service_bootstrap import prepare
            result = prepare(client, node=node, vmid=int(vmid), name=name, instance_id=instance_id,
                             spec=dict(spec, prepare_identity=True),
                             heartbeat=lambda: heartbeat(worker_id, "running", operation_id, phase="dhcp_guest_validation"))
            if result:
                emit(operation_id, "guest.identity.verified", "succeeded", json.dumps({"ipv4": result.get("address"), "mode": "dhcp-qga"}))
            return result
        return None
    if not started:
        emit(operation_id, "guest.identity.deferred", "pending", "se ejecutará al encender la VM")
        return None
    try:
        emit(operation_id, "guest.identity.started", "running", "Ubuntu 24.04; DHCP identificado por MAC")
        result = ensure_guest_identity(
            client,
            node=node,
            vmid=int(vmid),
            name=name,
            instance_id=instance_id,
            heartbeat=lambda: heartbeat(worker_id, "running", operation_id, phase="guest_identity"),
        )
        from worker.service_bootstrap import prepare
        binding = prepare(client, node=node, vmid=int(vmid), name=name, instance_id=instance_id,
                          heartbeat=lambda: heartbeat(worker_id, "running", operation_id, phase="service_bootstrap")) if spec.get("dns_servers") else None
        if binding:
            emit(operation_id, "service.binding.prepared", "succeeded", "Identidad SSH y resolvers verificados por QEMU Guest Agent")
        detail = {"ipv4": result.get("ipv4"), "mode": result.get("mode")}
        emit(operation_id, "guest.identity.verified", "succeeded", json.dumps(detail, sort_keys=True))
        return result
    except GuestIdentityError as error:
        try:
            emit(operation_id, "guest.identity.uncertain", "uncertain", str(error))
        except Exception:
            pass
        raise
    except Exception as error:
        raise GuestIdentityError("GUEST_IDENTITY_AUDIT_FAILED") from error


def ensure_vm_running_if_requested(
    worker_id: str,
    operation_id: str,
    client: ProxmoxClient,
    *,
    node: str,
    vmid: int,
    name: str,
    started: bool,
    expected: dict | None = None,
    proof_operation_id: str | None = None,
) -> dict | None:
    """Apply the requested power state only after a complete stopped-state gate."""
    if not isinstance(expected, dict):
        raise ProxmoxError("PREBOOT_EXPECTATION_REQUIRED")
    instance = expected.get("instance") or {}
    dns_servers = instance.get("dns_servers") or []
    if dns_servers:
        config = client.ensure_cloudinit_dns(node, vmid, name, dns_servers)
        emit(operation_id, "cloudinit.dns.verified", "succeeded", config["nameserver"])
    gate_already_passed = preboot_previously_verified(proof_operation_id or operation_id)
    observed = verify_preboot_configuration(
        operation_id,
        client,
        node=node,
        vmid=vmid,
        name=name,
        expected=expected,
        require_stopped=not (started and gate_already_passed),
        proof_operation_id=proof_operation_id,
    )
    if not started:
        return None
    power_state = observed.get("power_state")
    if power_state == "running" and gate_already_passed:
        return observed
    if power_state != "stopped":
        raise ProxmoxError("PROVISION_POWER_STATE_UNCONFIRMED", uncertain=True)

    # Repeat the read-only gate immediately before the Proxmox power request.
    # Any concurrent drift leaves the clone stopped and the operation failed.
    observed = verify_preboot_configuration(
        operation_id,
        client,
        node=node,
        vmid=vmid,
        name=name,
        expected=expected,
        require_stopped=not gate_already_passed,
        record=False,
    )
    if observed.get("power_state") == "running" and gate_already_passed:
        return observed
    if observed.get("power_state") != "stopped":
        raise ProxmoxError("PROVISION_POWER_STATE_UNCONFIRMED", uncertain=True)
    heartbeat(worker_id, "running", operation_id, phase="provision_recovery_start")
    emit(operation_id, "provision.power.start.started", "running", f"VMID {vmid} estaba detenida")
    result = client.power(node, vmid, name, "start")
    if result.get("power_state") != "running":
        raise ProxmoxError("PROVISION_POWER_START_UNCONFIRMED", uncertain=True)
    emit(operation_id, "provision.power.start.completed", "succeeded", f"VMID {vmid} está ejecutándose")
    return result


def verify_power_preconditions(
    operation_id: str,
    client: ProxmoxClient,
    *,
    node: str,
    vmid: int,
    name: str,
    action: str,
    expected: dict,
) -> dict:
    """Releer la configuración completa justo antes de iniciar o reiniciar."""

    if action not in {"start", "reboot"}:
        raise ProxmoxError("POWER_ACTION_INVALID")
    required_state = "stopped" if action == "start" else "running"
    observed = verify_preboot_configuration(
        operation_id,
        client,
        node=node,
        vmid=vmid,
        name=name,
        expected=expected,
        require_stopped=(action == "start"),
        record=False,
    )
    if observed.get("power_state") != required_state:
        raise ProxmoxError("PROVISION_POWER_STATE_UNCONFIRMED", uncertain=True)
    return observed


def guest_identity_previously_verified(instance_id: str) -> bool:
    """Return true only when a successful identity check is in the audit log."""

    with database() as db:
        row = db.execute(
            """
            SELECT 1
            FROM operations AS operation
            JOIN events AS event ON event.operation_id = operation.operation_id
            WHERE operation.instance_id = ?
              AND event.kind = 'guest.identity.verified'
              AND event.status = 'succeeded'
            LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
    return row is not None


def preboot_previously_verified(operation_id: str | None) -> bool:
    """A recovered running clone is accepted only if its own gate was audited."""

    if not operation_id:
        return False
    with database() as db:
        row = db.execute(
            """
            SELECT 1 FROM events
            WHERE operation_id=? AND kind='kvm.preboot.verified' AND status='succeeded'
            LIMIT 1
            """,
            (operation_id,),
        ).fetchone()
    return row is not None


def release_operation_lease(vmid: int | None, source_operation: dict, *, connection=None) -> None:
    owner = source_operation.get("plan_operation_id") or source_operation.get("operation_id")
    if vmid is not None and owner:
        release(int(vmid), owner, connection=connection)


def finalize_operation_failure(
    worker_id: str,
    operation_id: str,
    command: str,
    instance_id: str | None,
    vmid: int | None,
    node: str | None,
    *,
    status: str,
    error: str,
    phase: str,
    exit_code: int | None = None,
    output: str = "",
    power_uncertain: bool = False,
    preserve_instance: bool = False,
    release_lease_for: dict | None = None,
) -> None:
    """Cierra operación, instancia, lease y evento en una única transacción."""
    timestamp = now()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            fence = _FENCE.get()
            if fence is not None:
                if fence != (worker_id, operation_id):
                    raise OperationFencedError("OPERATION_FINALIZATION_FENCE_MISMATCH")
                assert_active_operation_owned(db, *fence)
            update_operation(
                operation_id,
                connection=db,
                status=status,
                phase=phase,
                finished_at=timestamp,
                last_heartbeat=timestamp,
                exit_code=exit_code,
                output=operation_output(output),
                error=operation_output(error),
            )
            if not preserve_instance:
                if command == "power" and not power_uncertain:
                    update_instance(
                        instance_id, "ready", vmid=vmid, node=node, connection=db
                    )
                else:
                    update_instance(
                        instance_id,
                        status,
                        vmid=vmid,
                        power_state="unknown" if command == "power" else None,
                        node=node,
                        connection=db,
                    )
            if release_lease_for is not None:
                release_operation_lease(vmid, release_lease_for, connection=db)
            emit(
                operation_id,
                f"terraform.{command}.failed",
                status,
                error,
                connection=db,
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise


def settle_physical_operation(
    operation_id: str,
    command: str,
    instance_id: str | None,
    vmid: int | None,
    node: str | None,
    source_payload: dict,
    source_operation: dict,
    *,
    output: str = "",
    recovered: bool = False,
) -> bool:
    ipam_settled = command != "destroy" or _release_static_allocation(instance_id, operation_id)
    status = "succeeded" if ipam_settled else "uncertain"
    error = "" if ipam_settled else "IPAM_RELEASE_PENDING"
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        fence = _FENCE.get()
        if fence is not None:
            ensure_operation_owned(db, fence[1])
        update_operation(
            operation_id,
            status=status,
            phase="completed" if ipam_settled else "recovery_required",
            finished_at=now(),
            last_heartbeat=now(),
            exit_code=0 if ipam_settled else 1,
            output=operation_output(output),
            error=error,
            connection=db,
        )
        if command == "destroy":
            update_instance(
                instance_id, "deleted" if ipam_settled else "uncertain",
                vmid=vmid, power_state="unknown", node=node, connection=db,
            )
            if ipam_settled:
                release_operation_lease(vmid, source_operation, connection=db)
        elif command in {"apply", "provision"}:
            started = bool(source_payload.get("spec", {}).get("started", True))
            update_instance(
                instance_id, "ready", vmid=vmid,
                power_state="running" if started else "stopped", node=node,
                connection=db,
            )
        detail = error or ("physical state reconciled" if recovered else "")
        outcome = "completed" if ipam_settled else "failed"
        emit(operation_id, f"terraform.{command}.{outcome}", status, detail, connection=db)
        db.execute("COMMIT")
    if command in {"destroy", "provision"}:
        delete_secret(source_operation.get("secret_path"))
    return ipam_settled


def close_source_after_reconciliation(
    source_operation: dict,
    instance_id: str | None,
    vmid: int | None,
    node: str | None,
    source_payload: dict,
    *,
    succeeded: bool,
    detail: str,
) -> bool:
    source_id = source_operation["operation_id"]
    if succeeded:
        return settle_physical_operation(
            source_id,
            source_operation["command"],
            instance_id,
            vmid,
            node,
            source_payload,
            source_operation,
            output=detail,
            recovered=True,
        )
    else:
        released = True
        if source_operation["command"] in {"apply", "provision", "plan"}:
            released = _release_static_allocation(instance_id, source_id)
        terminal_status = "failed" if released else "uncertain"
        terminal_error = detail if released else "IPAM_RELEASE_PENDING"
        # Cerrar una intención anterior no revierte una eliminación posterior.
        # Si queda IPAM pendiente, la incertidumbre sigue siendo visible.
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            fence = _FENCE.get()
            if fence is not None:
                assert_active_operation_owned(db, *fence)
            update_operation(
                source_id, status=terminal_status,
                finished_at=now(), last_heartbeat=now(), exit_code=1,
                output="", error=terminal_error, connection=db,
            )
            db.execute(
                """UPDATE instances SET status=?, vmid=COALESCE(?,vmid),
                node=COALESCE(?,node), updated_at=? WHERE instance_id=?
                AND (status!='deleted' OR ?='uncertain')""",
                (terminal_status, vmid, node, now(), instance_id, terminal_status),
            )
            if released and source_operation["command"] in {"apply", "provision", "plan"}:
                release_operation_lease(vmid, source_operation, connection=db)
            emit(source_id, "operation.reconciled", terminal_status, terminal_error, connection=db)
            db.execute("COMMIT")
        return released


def close_superseded_reconcile_attempts(
    instance_id: str, source_operation_id: str, detail: str
) -> None:
    """Close stale recovery attempts after their source has a confirmed successor."""

    with database() as db:
        rows = db.execute(
            """
            SELECT operation_id, payload_json FROM operations
            WHERE instance_id=? AND command='reconcile' AND status='uncertain'
            """,
            (instance_id,),
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if payload.get("source_operation_id") != source_operation_id:
            continue
        update_operation(
            row["operation_id"],
            status="failed",
            phase="superseded",
            finished_at=now(),
            last_heartbeat=now(),
            exit_code=1,
            output=detail,
            error="RECONCILIATION_SUPERSEDED_BY_CONFIRMED_LIFECYCLE",
        )
        emit(
            row["operation_id"],
            "operation.superseded_by_lifecycle",
            "failed",
            detail,
        )


def fail_present_source_after_identity_error(
    source_operation: dict,
    recovery_operation_id: str,
    instance_id: str | None,
    vmid: int,
    node: str,
    power_state: str,
    error: GuestIdentityError,
) -> None:
    """Close a failed guest validation while retaining its VM and IP lease.

    Reconciliation has already proved that the physical VM exists. Releasing
    its NetBox address here would allow a second VM to claim an address that
    remains configured on this one. Mark the failed provisioning terminal,
    expose the confirmed power state, and require the normal API shutdown and
    destroy lifecycle to release the allocation after physical absence is
    confirmed.
    """
    if source_operation.get("command") not in {"apply", "provision"}:
        raise RuntimeError("RECONCILIATION_SOURCE_COMMAND_INVALID")
    if power_state not in {"running", "stopped"}:
        raise ProxmoxError("RECONCILIATION_POWER_STATE_UNCONFIRMED", uncertain=True)

    detail = operation_output(str(error))
    update_operation(
        source_operation["operation_id"],
        status="failed",
        phase="guest_identity_failed",
        finished_at=now(),
        last_heartbeat=now(),
        exit_code=1,
        error=detail,
    )
    update_instance(
        instance_id,
        "failed",
        vmid=vmid,
        power_state=power_state,
        node=node,
    )
    emit(
        source_operation["operation_id"],
        "guest.identity.reconciliation_failed",
        "failed",
        detail,
    )
    update_operation(
        recovery_operation_id,
        status="failed",
        phase="completed",
        finished_at=now(),
        last_heartbeat=now(),
        exit_code=1,
        output="La VM física existe; la IP queda retenida hasta completar apagado y eliminación por API.",
        error="RECONCILIATION_GUEST_IDENTITY_FAILED",
    )
    emit(
        recovery_operation_id,
        "recovery.completed",
        "failed",
        "La VM existe y conserva su asignación IP para limpieza controlada por API.",
    )


def execute_reconcile(worker_id: str, operation: dict, client: ProxmoxClient) -> None:
    operation_id = operation["operation_id"]
    payload = json.loads(operation["payload_json"])
    instance_id = payload.get("instance_id") or operation.get("instance_id")
    source_id = payload.get("source_operation_id")
    source = find_operation(source_id) if source_id else None
    if source is None and instance_id:
        with database() as db:
            row = db.execute(
                """
                SELECT * FROM operations
                WHERE instance_id=? AND status='uncertain' AND operation_id != ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (instance_id, operation_id),
            ).fetchone()
        source = dict(row) if row else None
    if source is None:
        raise RuntimeError("RECONCILIATION_SOURCE_NOT_FOUND")

    if source.get("instance_id") != instance_id:
        raise RuntimeError("OPERATION_INSTANCE_MISMATCH")
    identity = instance_identity(instance_id)
    instance_id = identity.get("instance_id") or source.get("instance_id")
    vmid = identity.get("vmid") or source.get("vmid")
    if source.get("vmid") and identity.get("vmid") and source["vmid"] != identity["vmid"]:
        raise RuntimeError("OPERATION_INSTANCE_MISMATCH")
    if is_protected_vmid(vmid):
        raise RuntimeError("PROTECTED_INSTANCE")
    node = identity.get("node") or source.get("target_node") or "mng01"
    name = identity.get("name")
    source_payload = json.loads(source.get("payload_json") or "{}")
    source_command = source.get("command")
    update_operation(operation_id, vmid=vmid, target_node=node)
    emit(operation_id, "recovery.started", "running", source_command or "operation")
    setter = getattr(client, "set_heartbeat", None)
    if setter is not None:
        setter(lambda: heartbeat(worker_id, "running", operation_id, phase="reconcile"))
    heartbeat(worker_id, "running", operation_id, phase="reconcile")

    if vmid is None or source_command == "plan":
        if vmid is not None and not physical_presence(client, int(vmid), False):
            raise ProxmoxError("RECONCILIATION_PHYSICAL_RESOURCE_PRESENT", uncertain=True)
        if not close_source_after_reconciliation(
            source, instance_id, vmid, node, source_payload,
            succeeded=source_command == "destroy",
            detail="No había VM física para reconciliar.",
        ):
            raise ProxmoxError("IPAM_RELEASE_PENDING", uncertain=True)
        update_operation(
            operation_id,
            status="succeeded" if source_command == "destroy" else "failed",
            finished_at=now(),
            last_heartbeat=now(),
            exit_code=0 if source_command == "destroy" else 1,
            output="No había VM física para reconciliar.",
            error="" if source_command == "destroy" else "RECONCILIATION_PHYSICAL_RESOURCE_ABSENT",
        )
        emit(operation_id, "recovery.completed", "succeeded" if source_command == "destroy" else "failed")
        return

    if identity.get("status") == "deleted" and source_command == "power":
        absent = reconcile_physical(
            client,
            int(vmid),
            False,
            name=name,
            node=node,
            heartbeat_callback=lambda: heartbeat(
                worker_id, "running", operation_id, phase="reconcile"
            ),
        )
        if not absent:
            raise ProxmoxError("DELETED_INSTANCE_PHYSICAL_RESOURCE_PRESENT", uncertain=True)
        with database() as db:
            superseded = db.execute(
                """
                SELECT source.* FROM operations AS source
                WHERE source.instance_id=? AND source.command='power'
                  AND source.status='uncertain'
                  AND EXISTS (
                    SELECT 1 FROM operations AS destroy
                    WHERE destroy.instance_id=source.instance_id
                      AND destroy.command='destroy' AND destroy.status='succeeded'
                      AND destroy.created_at > source.created_at
                  )
                ORDER BY source.created_at
                """,
                (instance_id,),
            ).fetchall()
        sources = [dict(row) for row in superseded]
        if not any(row["operation_id"] == source["operation_id"] for row in sources):
            raise RuntimeError("DELETED_POWER_SOURCE_NOT_SUPERSEDED")
        detail = (
            f"La VMID está ausente tras una eliminación confirmada; "
            f"{len(sources)} solicitudes históricas de energía quedaron superadas."
        )
        for old_source in sources:
            old_payload = json.loads(old_source.get("payload_json") or "{}")
            close_source_after_reconciliation(
                old_source,
                instance_id,
                int(vmid),
                node,
                old_payload,
                succeeded=False,
                detail=detail,
            )
            emit(
                old_source["operation_id"],
                "operation.superseded_by_destroy",
                "failed",
                detail,
            )
            close_superseded_reconcile_attempts(
                instance_id, old_source["operation_id"], detail
            )
        update_operation(
            operation_id,
            status="succeeded",
            phase="completed",
            finished_at=now(),
            last_heartbeat=now(),
            exit_code=0,
            output=detail,
            error="",
        )
        emit(operation_id, "recovery.completed", "succeeded", detail)
        return

    if source_command == "power":
        if _FENCE.get() is not None:
            assert_physical_identity(client, int(vmid), node, name, identity.get("operation_id"))
        action = source_payload.get("power_action")
        if action not in {"start", "shutdown", "reboot"}:
            raise RuntimeError("POWER_ACTION_INVALID")
        with database() as db:
            successor = db.execute(
                """
                SELECT operation_id FROM operations
                WHERE instance_id=? AND command='power' AND status='succeeded'
                  AND created_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (instance_id, source.get("created_at") or ""),
            ).fetchone()
        if successor is not None:
            spec = instance_spec(instance_id)
            expected = expected_preboot_for_spec(
                spec,
                vmid=int(vmid),
                node=node,
                client=client,
                stack=source.get("stack") or "mng01",
                password_present=instance_password_was_provisioned(instance_id),
            )
            observed = verify_preboot_configuration(
                operation_id,
                client,
                node=node,
                vmid=int(vmid),
                name=name,
                expected=expected,
                require_stopped=False,
            )
            power_state = observed.get("power_state")
            if power_state not in {"running", "stopped"}:
                raise ProxmoxError("POWER_STATE_UNCONFIRMED", uncertain=True)
            if power_state == "running":
                ensure_guest_identity_for_operation(
                    worker_id,
                    operation_id,
                    client,
                    stack=source.get("stack") or "mng01",
                    node=node,
                    vmid=int(vmid),
                    name=name,
                    instance_id=instance_id,
                    spec=spec,
                    started=True,
                )
            detail = (
                "La operación histórica quedó superada por una operación posterior "
                "confirmada; se verificaron la configuración y el estado físico actuales."
            )
            update_instance(
                instance_id,
                "ready",
                vmid=int(vmid),
                power_state=power_state,
                node=node,
            )
            update_operation(
                source["operation_id"],
                status="failed",
                phase="superseded",
                finished_at=now(),
                last_heartbeat=now(),
                exit_code=1,
                output="",
                error=detail,
            )
            emit(
                source["operation_id"],
                "operation.reconciled_by_later_lifecycle",
                "failed",
                detail,
            )
            close_superseded_reconcile_attempts(
                instance_id, source["operation_id"], detail
            )
            update_operation(
                operation_id,
                status="succeeded",
                phase="completed",
                finished_at=now(),
                last_heartbeat=now(),
                exit_code=0,
                output=detail,
                error="",
            )
            emit(operation_id, "recovery.completed", "succeeded", detail)
            return
        observed = client.power_state(node, int(vmid), name)
        if action in {"start", "reboot"}:
            spec = instance_spec(instance_id)
            expected = expected_preboot_for_spec(
                spec,
                vmid=int(vmid),
                node=node,
                client=client,
                stack=source.get("stack") or "mng01",
                password_present=instance_password_was_provisioned(instance_id),
            )
            dns_servers = expected.get("instance", {}).get("dns_servers") or []
            if dns_servers:
                client.ensure_cloudinit_dns(node, int(vmid), name, dns_servers)
            observed = verify_preboot_configuration(
                operation_id,
                client,
                node=node,
                vmid=int(vmid),
                name=name,
                expected=expected,
                require_stopped=False,
            )
            if (
                action == "start"
                and observed.get("power_state") == "running"
                and not preboot_previously_verified(source.get("operation_id"))
            ):
                raise ProxmoxError("POWER_START_WITHOUT_PREBOOT_PROOF", uncertain=True)
        desired = "running" if action == "start" else "stopped"
        if action in {"start", "shutdown"} and observed.get("power_state") == desired:
            result = {"action": action, "already": True, **observed}
        elif action == "reboot":
            # execute_power reaches guest identity only after Proxmox accepted
            # the reboot and reported the VM running. If identity was already
            # verified during provisioning, a temporary post-reboot Guest
            # Agent outage must not trigger a second reboot during recovery.
            if (
                observed.get("power_state") == "running"
                and source.get("error") == "GUEST_IDENTITY_AGENT_UNAVAILABLE"
                and guest_identity_previously_verified(instance_id)
            ):
                result = {"action": action, "already": True, **observed}
            else:
                proof = getattr(client, "completed_power_task_during", None)
                task = proof(node, int(vmid), "reboot", source.get("started_at"), source.get("finished_at")) if proof else None
                if not task or observed.get("power_state") != "running":
                    raise ProxmoxError("POWER_REBOOT_REQUIRES_REVIEW", uncertain=True)
                ensure_guest_identity_for_operation(worker_id, operation_id, client, stack=source.get("stack") or "mng01",
                                                    node=node, vmid=int(vmid), name=name, instance_id=instance_id,
                                                    spec=instance_spec(instance_id))
                result = {"action": action, "already": True, "verified_task": task, **observed}
        else:
            if action in {"start", "reboot"}:
                verify_power_preconditions(
                    operation_id,
                    client,
                    node=node,
                    vmid=int(vmid),
                    name=name,
                    action=action,
                    expected=expected,
                )
            result = client.power(node, int(vmid), name, action)
        if action == "start" and result.get("power_state") == "running":
            ensure_guest_identity_for_operation(
                worker_id,
                operation_id,
                client,
                stack=source.get("stack") or "mng01",
                node=node,
                vmid=int(vmid),
                name=name,
                instance_id=instance_id,
                spec=instance_spec(instance_id),
            )
        update_instance(instance_id, "ready", vmid=int(vmid), power_state=result.get("power_state"), node=node)
        close_source_after_reconciliation(
            source, instance_id, int(vmid), node, source_payload,
            succeeded=True,
            detail="La energía física fue reconciliada.",
        )
        update_operation(
            operation_id,
            status="succeeded",
            phase="completed",
            finished_at=now(),
            last_heartbeat=now(),
            exit_code=0,
            output=json.dumps(result, sort_keys=True),
            error="",
        )
        emit(operation_id, "recovery.completed", "succeeded")
        return

    if source_command not in {"apply", "provision", "destroy"}:
        raise RuntimeError("RECONCILIATION_COMMAND_UNSUPPORTED")

    expected = source_command != "destroy"
    present = reconcile_physical(
        client,
        int(vmid),
        expected,
        name=name,
        node=node,
        heartbeat_callback=lambda: heartbeat(worker_id, "running", operation_id, phase="reconcile"),
    )
    if present:
        if source_command in {"apply", "provision"}:
            spec = operation_spec(source, source_payload)
            started = bool(spec.get("started", True))
            source_stack = source.get("stack") or "mng01"
            source_workspace = os.path.join(
                source.get("workspace_path") or os.path.join(data_root(), "plans", source.get("plan_operation_id") or source.get("operation_id"), "terraform"),
                source_stack_directory(source_stack),
            )
            if _FENCE.get() is not None:
                assert_physical_identity(client, int(vmid), node, name, identity.get("operation_id"))
                require_reconciled_terraform_state(source_workspace, int(vmid))
            expected_preboot = operation_preboot_expectation(
                source_workspace,
                spec,
                vmid=int(vmid),
                node=node,
                client=client,
                stack=source_stack,
                password_present=bool(source.get("secret_path")),
            )
            password_source = source
            if source.get("plan_operation_id"):
                password_source = find_operation(source["plan_operation_id"]) or source
            configure_workspace_password(
                client, source_workspace,
                proof_operation_id=source["operation_id"],
                node=node, vmid=int(vmid), name=name, expected=expected_preboot,
                secret_path=password_source.get("secret_path"),
            )
            observed = ensure_vm_running_if_requested(
                worker_id,
                operation_id,
                client,
                node=node,
                vmid=int(vmid),
                name=name,
                started=started,
                expected=expected_preboot,
                proof_operation_id=source.get("operation_id"),
            )
            try:
                ensure_guest_identity_for_operation(
                    worker_id,
                    operation_id,
                    client,
                    stack=source.get("stack") or "mng01",
                    node=node,
                    vmid=int(vmid),
                    name=name,
                    instance_id=instance_id,
                    spec=spec,
                    started=started,
                )
            except GuestIdentityError as error:
                power = observed.get("power_state") if isinstance(observed, dict) else None
                if power not in {"running", "stopped"}:
                    power = client.power_state(node, int(vmid), name).get("power_state")
                fail_present_source_after_identity_error(
                    source,
                    operation_id,
                    instance_id,
                    int(vmid),
                    node,
                    power,
                    error,
                )
                return
        if not close_source_after_reconciliation(
            source, instance_id, int(vmid), node, source_payload,
            succeeded=True,
            detail="La presencia física coincide con la operación solicitada.",
        ):
            raise ProxmoxError("IPAM_RELEASE_PENDING", uncertain=True)
        update_operation(
            operation_id,
            status="succeeded",
            finished_at=now(),
            last_heartbeat=now(),
            exit_code=0,
            output="La presencia física fue reconciliada.",
            error="",
        )
        emit(operation_id, "recovery.completed", "succeeded")
        return

    if source_command == "destroy":
        source_stack = source.get("stack") or "mng01"
        source_workspace = (
            os.path.join(source["workspace_path"], source_stack_directory(source_stack))
            if source.get("workspace_path")
            else None
        )
        if source_stack == "mng01" and remove_orphan_vm(
            client,
            operation_id,
            instance_id,
            int(vmid),
            source_workspace,
            heartbeat_callback=lambda: heartbeat(worker_id, "running", operation_id, phase="reconcile"),
        ):
            detail = "VM huérfana fuera del state de Terraform eliminada con identidad verificada."
            if not close_source_after_reconciliation(
                source, instance_id, int(vmid), node, source_payload,
                succeeded=True,
                detail=detail,
            ):
                raise ProxmoxError("IPAM_RELEASE_PENDING", uncertain=True)
            update_operation(
                operation_id,
                status="succeeded",
                finished_at=now(),
                last_heartbeat=now(),
                exit_code=0,
                output=detail,
                error="",
            )
            emit(operation_id, "recovery.completed", "succeeded")
            return
        raise ProxmoxError("RECONCILIATION_REQUIRED", uncertain=True)

    if not close_source_after_reconciliation(
        source, instance_id, int(vmid), node, source_payload,
        succeeded=False,
        detail="RECONCILIATION_PHYSICAL_RESOURCE_ABSENT",
    ):
        raise ProxmoxError("IPAM_RELEASE_PENDING", uncertain=True)
    update_operation(
        operation_id,
        status="failed",
        finished_at=now(),
        last_heartbeat=now(),
        exit_code=1,
        output="",
        error="RECONCILIATION_PHYSICAL_RESOURCE_ABSENT",
    )
    emit(operation_id, "recovery.completed", "failed")


def execute_power(worker_id: str, operation: dict, client: ProxmoxClient) -> None:
    operation_id = operation["operation_id"]
    payload = json.loads(operation["payload_json"])
    action = payload.get("power_action")
    instance_id = payload.get("instance_id") or operation.get("instance_id")
    with database() as db:
        row = db.execute(
            "SELECT instance_id, operation_id, vmid, node, name, status, spec_json FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
    if row is None or row["vmid"] is None:
        raise RuntimeError("POWER_INSTANCE_NOT_PROVISIONED")
    if row["status"] == "deleted":
        raise RuntimeError("POWER_INSTANCE_DELETED")
    if action not in {"start", "shutdown", "reboot"}:
        raise RuntimeError("POWER_ACTION_INVALID")

    vmid = int(row["vmid"])
    if is_protected_vmid(vmid):
        raise RuntimeError("PROTECTED_INSTANCE")
    node = row["node"] or "mng01"
    setter = getattr(client, "set_heartbeat", None)
    if setter is not None:
        setter(lambda: heartbeat(worker_id, "running", operation_id, phase=f"power_{action}"))
    try:
        spec = json.loads(row["spec_json"] or "{}")
    except (TypeError, ValueError):
        spec = {}
    if _FENCE.get() is not None:
        assert_physical_identity(client, vmid, node, row["name"], row["operation_id"])
    if action in {"start", "reboot"}:
        expected = expected_preboot_for_spec(
            spec,
            vmid=vmid,
            node=node,
            client=client,
            stack=operation.get("stack") or "mng01",
            password_present=instance_password_was_provisioned(instance_id),
        )
        dns_servers = expected.get("instance", {}).get("dns_servers") or []
        if dns_servers:
            client.ensure_cloudinit_dns(node, vmid, row["name"], dns_servers)
        verify_preboot_configuration(
            operation_id,
            client,
            node=node,
            vmid=vmid,
            name=row["name"],
            expected=expected,
            require_stopped=(action == "start"),
        )
    update_operation(operation_id, vmid=vmid)
    update_instance(instance_id, "powering", vmid=vmid, power_state="unknown")
    emit(operation_id, f"power.{action}.started", "running")
    heartbeat(worker_id, "running", operation_id, phase=f"power_{action}")
    if action in {"start", "reboot"}:
        verify_power_preconditions(
            operation_id,
            client,
            node=node,
            vmid=vmid,
            name=row["name"],
            action=action,
            expected=expected,
        )
    result = client.power(node, vmid, row["name"], action)
    power_state = result.get("power_state")
    expected_power_state = "stopped" if action == "shutdown" else "running"
    if power_state != expected_power_state:
        raise ProxmoxError("POWER_RESULT_UNCONFIRMED", uncertain=True)
    if _FENCE.get() is not None:
        observation_deadline = time.monotonic() + 30
        while client.power_state(node, vmid, row["name"])["power_state"] != expected_power_state:
            if time.monotonic() >= observation_deadline:
                raise ProxmoxError("POWER_RESULT_UNCONFIRMED", uncertain=True)
            heartbeat(worker_id, "running", operation_id, phase=f"power_{action}_observing")
            time.sleep(1)
    if (
        action in {"start", "reboot"}
        and power_state == "running"
        and not guest_identity_previously_verified(instance_id)
    ):
        ensure_guest_identity_for_operation(
            worker_id,
            operation_id,
            client,
            stack=operation.get("stack") or "mng01",
            node=node,
            vmid=vmid,
            name=row["name"],
            instance_id=instance_id,
            spec=spec if isinstance(spec, dict) else {},
        )
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        update_operation(
            operation_id,
            status="succeeded",
            finished_at=now(),
            last_heartbeat=now(),
            exit_code=0,
            output=json.dumps(result, sort_keys=True),
            error="",
            connection=db,
        )
        update_instance(instance_id, "ready", vmid=vmid, power_state=power_state, connection=db)
        emit(operation_id, f"power.{action}.completed", "succeeded", connection=db)
        db.execute("COMMIT")


def execute(worker_id: str, operation: dict) -> None:
    operation_id = operation["operation_id"]
    command = "unknown"
    phase = "plan"
    stack = operation.get("stack") or "mng01"
    instance_id = operation.get("instance_id")
    vmid: int | None = operation.get("vmid")
    target_node = operation.get("target_node")
    pool_id: str | None = None
    allocated_here = False
    client: ProxmoxClient | None = None
    source_operation: dict = operation
    source_payload: dict = {}
    secret_path: str | None = operation.get("secret_path")
    requested_started: bool | None = None
    credential_scope = _PROXMOX_CREDENTIALS.set(None)
    try:
        payload = json.loads(operation["payload_json"])
        command = payload["command"]
        source_payload = payload
        stack = payload.get("stack") or stack
        if instance_id and payload.get("instance_id") not in {None, instance_id}:
            raise RuntimeError("OPERATION_INSTANCE_MISMATCH")
        instance_id = instance_id or payload.get("instance_id")

        if command == "power":
            if stack != "mng01":
                raise RuntimeError("POWER_STACK_UNSUPPORTED")
            load_environment()
            client = ProxmoxClient()
            execute_power(worker_id, operation, client)
            return

        if command == "reconcile":
            phase = "reconcile"
            if stack != "mng01":
                raise RuntimeError("RECONCILIATION_STACK_UNSUPPORTED")
            load_environment()
            client = ProxmoxClient()
            execute_reconcile(worker_id, operation, client)
            return

        source_operation_id = operation_id
        source_operation = operation

        if command in {"apply", "destroy"}:
            source_operation_id = payload.get("plan_operation_id")
            if not source_operation_id and instance_id:
                source_operation_id = instance_plan_operation(instance_id)
            if not source_operation_id:
                raise RuntimeError(f"{command} requiere una operacion de plan")
            source_operation = find_operation(source_operation_id)
            if source_operation is None:
                raise RuntimeError(f"no existe el plan {source_operation_id}")
            source_payload = json.loads(source_operation["payload_json"])
            stack = source_payload.get("stack") or source_operation.get("stack") or stack
            if not instance_id or source_operation.get("instance_id") != instance_id:
                raise RuntimeError("OPERATION_INSTANCE_MISMATCH")
            target_node = source_operation.get("target_node") or target_node
            vmid = source_operation.get("vmid")
            if vmid is None and instance_id:
                with database() as db:
                    row = db.execute(
                        "SELECT vmid FROM instances WHERE instance_id=?", (instance_id,)
                    ).fetchone()
                vmid = row["vmid"] if row else None
            if is_protected_vmid(vmid):
                raise RuntimeError("PROTECTED_INSTANCE")
            identity = instance_identity(instance_id)
            if identity.get("operation_id") != source_operation_id or (
                vmid is not None and identity.get("vmid") != vmid
            ):
                raise RuntimeError("OPERATION_INSTANCE_MISMATCH")
        requested_started = bool(source_payload.get("spec", {}).get("started", True))

        source_path = os.getenv("IACP_TERRAFORM_PATH", "/workspace/terraform")
        if stack == "mng01":
            load_environment()
            client = ProxmoxClient()
            setter = getattr(client, "set_heartbeat", None)
            if setter is not None:
                setter(lambda: heartbeat(worker_id, "running", operation_id, phase=phase))

        secret_path = secret_path_for_command(command, source_operation, operation)

        if command in {"plan", "provision"}:
            from api.network_policy import selected_profile, wan_only
            if wan_only() and (payload.get("spec") or {}).get("network_profile", "lab_dhcp") != selected_profile():
                raise RuntimeError("NETWORK_POLICY_MISMATCH")
            spec = payload.get("spec")
            if not spec:
                raise RuntimeError(f"{command} requiere spec")
            if stack == "mng01":
                pool_id = validate_pool(client)
                emit(operation_id, "proxmox.pool.validated", "ok", pool_id)
                golden = observe(spec["image"], client)
                emit(operation_id, "golden.observed", "ok", spec["image"])
                requested_started = bool(spec.get("started", True))
                target_node = target_node or select_target_node(client)
                check_owner = (
                    partial(assert_active_operation_owned, worker_id=worker_id, operation_id=operation_id)
                    if _FENCE.get() is not None else None
                )
                vmid = allocate(operation_id, client, node=target_node, check_owner=check_owner)
                allocated_here = True
                spec = _ensure_static_allocation(
                    worker_id=worker_id,
                    operation_id=operation_id,
                    instance_id=instance_id,
                    vmid=int(vmid),
                    payload=payload,
                    source_operation=operation,
                )
                values = golden_variables(
                    spec,
                    vmid,
                    golden,
                    stack,
                    target_node=target_node,
                    allowed_nodes=cluster_nodes(),
                    has_root_password=read_secret(secret_path) is not None,
                    pool_id=pool_id,
                )
                marker = f"iacp-owned-{operation_id}"
                values["instance"]["tags"] = [
                    tag for tag in values["instance"].get("tags", [])
                    if not tag.startswith("iacp-owned-")
                ] + [marker]
                payload["physical_identity_tag"] = marker
                _update_operation_payload(operation_id, payload)
                update_instance(instance_id, "planning", vmid=vmid, node=target_node)
                emit(operation_id, "vmid.reserved", "held", str(vmid))
                expected = preboot_expectation(
                    values["instance"],
                    golden,
                    password_present=bool(secret_path),
                    node=target_node,
                    vmid=int(vmid),
                )
                if secret_path:
                    values["password_configured_by_worker"] = True
                    expected["password_configured_by_worker"] = True
            else:
                values = local_variables(spec)
                update_instance(instance_id, "planning")
            workspace_root, workspace = create_workspace(operation_id, source_path, stack)
            write_variables(workspace, values)
            if stack == "mng01" and command in {"plan", "provision"}:
                save_preboot_expectation(workspace, expected)
            plan_path = os.path.join(data_root(), "plans", operation_id, "plan.tfplan")
            update_operation(
                operation_id,
                workspace_path=workspace_root,
                plan_path=plan_path,
                vmid=vmid,
                target_node=target_node,
            )
        else:
            workspace_root = source_operation.get("workspace_path") or os.path.join(
                data_root(), "plans", source_operation_id, "terraform"
            )
            workspace = os.path.join(workspace_root, source_stack_directory(stack))
            if command == "destroy" and stack == "mng01" and client is not None:
                identity = instance_identity(instance_id)
                if identity.get("status") == "deleted":
                    raise RuntimeError("OPERATION_INSTANCE_ALREADY_DELETED")
                record = physical_record(client, int(vmid)) if vmid is not None else None
                if record is None:
                    settle_physical_operation(
                        operation_id, command, instance_id, vmid, target_node,
                        source_payload, source_operation,
                    )
                    return
                name = identity.get("name")
                node = target_node or identity.get("node")
                if not name or not node or record.get("node") != node:
                    raise ProxmoxError("VM_PHYSICAL_OWNER_MISMATCH", uncertain=True)
                assert_destroy_physical_identity(
                    client,
                    int(vmid),
                    node,
                    name,
                    source_operation_id,
                    workspace,
                )
                if client.power_state(node, int(vmid), name)["power_state"] != "stopped":
                    raise RuntimeError("DESTROY_REQUIRES_STOPPED_INSTANCE")
            if not os.path.isdir(workspace):
                raise RuntimeError(f"no existe el workspace del plan {source_operation_id}")
            plan_path = source_operation.get("plan_path") or os.path.join(
                data_root(), "plans", source_operation_id, "plan.tfplan"
            )
            vmid = source_operation.get("vmid")
            target_node = source_operation.get("target_node") or target_node
            if vmid is None and instance_id:
                with database() as db:
                    row = db.execute(
                        "SELECT vmid, node FROM instances WHERE instance_id=?", (instance_id,)
                    ).fetchone()
                vmid = row["vmid"] if row else None
                target_node = target_node or (row["node"] if row else None)
            update_operation(
                operation_id,
                vmid=vmid,
                workspace_path=workspace_root,
                plan_path=plan_path,
                target_node=target_node,
            )
            if command == "apply":
                phase = "apply"
                update_instance(instance_id, "applying", vmid=vmid, node=target_node)
            else:
                phase = "destroy"
                update_instance(instance_id, "destroying", vmid=vmid)

        if command == "provision":
            phase = "plan"
            update_operation(operation_id, phase=phase)
            emit(operation_id, "terraform.plan.started", "running")
            return_code, output, error = run_commands(
                worker_id,
                operation_id,
                workspace,
                terraform_commands("plan", plan_path),
                secret_path=secret_path,
                phase=phase,
            )
            if return_code == 0:
                emit(operation_id, "terraform.plan.completed", "succeeded")
                phase = "apply"
                update_operation(operation_id, phase=phase)
                if stack == "mng01":
                    assert_stopped_plan(workspace, plan_path, (int(vmid), spec["name"], target_node))
                    if _FENCE.get() is not None:
                        require_free_provision_target(client, int(vmid), operation_id)
                emit(operation_id, "terraform.apply.started", "running")
                apply_code, apply_output, apply_error = run_commands(
                    worker_id,
                    operation_id,
                    workspace,
                    [["terraform", "apply", "-input=false", "-no-color", plan_path]],
                    secret_path=secret_path,
                    phase=phase,
                )
                return_code = apply_code
                output = operation_output(output + apply_output)
                error = operation_output(error + apply_error)
        else:
            update_operation(operation_id, phase=phase)
            if command == "apply" and stack == "mng01":
                from api.network_policy import selected_profile, wan_only

                if wan_only() and source_payload.get("spec", {}).get("network_profile", "lab_dhcp") != selected_profile():
                    raise RuntimeError("NETWORK_POLICY_MISMATCH")
                assert_stopped_plan(
                    workspace, plan_path, (int(vmid), identity["name"], target_node or identity.get("node")),
                )
                if _FENCE.get() is not None:
                    require_free_provision_target(client, int(vmid), source_operation_id)
            emit(operation_id, f"terraform.{command}.started", "running")
            heartbeat(worker_id, "running", operation_id, phase=phase)
            return_code, output, error = run_commands(
                worker_id,
                operation_id,
                workspace,
                terraform_commands(command, plan_path),
                secret_path=secret_path,
                phase=phase,
            )
        if return_code != 0:
            physical = stack == "mng01" and phase in {"apply", "destroy"}
            failure_text = f"{output}\n{error}"
            recovered = False
            if physical and client is not None and vmid is not None and not deterministic_error(failure_text):
                emit(operation_id, "recovery.started", "running", "physical operation")
                identity = instance_identity(instance_id)
                recovered = reconcile_physical(
                    client,
                    int(vmid),
                    command in {"apply", "provision"},
                    name=identity.get("name"),
                    node=target_node or identity.get("node"),
                    heartbeat_callback=lambda: heartbeat(worker_id, "running", operation_id, phase="recovery"),
                )
                if recovered:
                    if command in {"apply", "provision"}:
                        spec = operation_spec(source_operation, source_payload)
                        started = requested_started if requested_started is not None else bool(spec.get("started", True))
                        recovery_node = target_node or identity.get("node") or "mng01"
                        if _FENCE.get() is not None:
                            assert_physical_identity(
                                client, int(vmid), recovery_node, identity.get("name"),
                                source_operation.get("operation_id"),
                            )
                            require_reconciled_terraform_state(workspace, int(vmid))
                        expected_preboot = operation_preboot_expectation(
                            workspace,
                            spec,
                            vmid=int(vmid),
                            node=recovery_node,
                            client=client,
                            stack=stack,
                            password_present=bool(secret_path),
                        )
                        configure_workspace_password(
                            client, workspace,
                            proof_operation_id=operation_id,
                            node=recovery_node, vmid=int(vmid), name=identity.get("name"),
                            expected=expected_preboot, secret_path=secret_path,
                        )
                        ensure_vm_running_if_requested(
                            worker_id,
                            operation_id,
                            client,
                            node=recovery_node,
                            vmid=int(vmid),
                            name=identity.get("name", ""),
                            started=started,
                            expected=expected_preboot,
                        )
                        ensure_guest_identity_for_operation(
                            worker_id,
                            operation_id,
                            client,
                            stack=stack,
                            node=recovery_node,
                            vmid=int(vmid),
                            name=instance_identity(instance_id).get("name", ""),
                            instance_id=instance_id,
                            spec=spec,
                            started=started,
                        )
                    settle_physical_operation(
                        operation_id,
                        command,
                        instance_id,
                        int(vmid),
                        target_node,
                        source_payload,
                        source_operation,
                        output=output,
                        recovered=True,
                    )
                    return

            status = "failed" if deterministic_error(failure_text) or not physical else "uncertain"
            ipam_release_ok = True
            if command in {"plan", "provision"} and (phase == "plan" or status == "failed"):
                ipam_release_ok = _release_static_allocation(instance_id, operation_id)
                if not ipam_release_ok:
                    status = "uncertain"
            terminal_error = "IPAM_RELEASE_PENDING" if not ipam_release_ok else error
            release_lease_for = (
                operation
                if command in {"plan", "provision", "apply"}
                and (phase == "plan" or status == "failed")
                and allocated_here
                and ipam_release_ok
                else None
            )
            finalize_operation_failure(
                worker_id,
                operation_id,
                command,
                instance_id,
                vmid,
                target_node,
                status=status,
                error=operation_output(terminal_error),
                phase="recovery_required" if status == "uncertain" else "failed",
                exit_code=return_code,
                output=output,
                release_lease_for=release_lease_for,
            )
            if command == "provision" and status != "uncertain":
                delete_secret(secret_path)
            return

        if stack == "mng01" and vmid is not None and client is not None:
            expected = command in {"apply", "provision"}
            identity = instance_identity(instance_id)
            if not reconcile_physical(
                client,
                int(vmid),
                expected,
                name=identity.get("name"),
                node=target_node or identity.get("node"),
                heartbeat_callback=lambda: heartbeat(worker_id, "running", operation_id, phase=phase),
            ) and not (
                command == "destroy"
                and remove_orphan_vm(
                    client,
                    operation_id,
                    instance_id,
                    int(vmid),
                    workspace,
                    heartbeat_callback=lambda: heartbeat(worker_id, "running", operation_id, phase=phase),
                )
            ):
                raise RuntimeError(
                    f"reconciliacion fisica fallida: VMID {vmid} no coincide con {expected}"
                )

        if command in {"apply", "provision"} and vmid is not None and client is not None:
            spec = operation_spec(source_operation, source_payload)
            started = requested_started if requested_started is not None else bool(spec.get("started", True))
            instance = instance_identity(instance_id)
            if _FENCE.get() is not None:
                assert_physical_identity(
                    client, int(vmid), target_node or instance.get("node") or "mng01",
                    instance.get("name"), source_operation.get("operation_id"),
                )
            expected_preboot = operation_preboot_expectation(
                workspace,
                spec,
                vmid=int(vmid),
                node=target_node or instance.get("node") or "mng01",
                client=client,
                stack=stack,
                password_present=bool(secret_path),
            )
            configure_workspace_password(
                client, workspace,
                proof_operation_id=operation_id,
                node=target_node or instance.get("node") or "mng01",
                vmid=int(vmid), name=instance.get("name"),
                expected=expected_preboot, secret_path=secret_path,
            )
            ensure_vm_running_if_requested(
                worker_id,
                operation_id,
                client,
                node=target_node or instance.get("node") or "mng01",
                vmid=int(vmid),
                name=instance.get("name", ""),
                started=started,
                expected=expected_preboot,
            )
            ensure_guest_identity_for_operation(
                worker_id,
                operation_id,
                client,
                stack=stack,
                node=target_node or instance.get("node") or "mng01",
                vmid=int(vmid),
                name=instance.get("name", ""),
                instance_id=instance_id,
                spec=spec,
                started=started,
            )

        if command != "plan":
            settle_physical_operation(
                operation_id, command, instance_id, vmid, target_node,
                source_payload, source_operation, output=output,
            )
            return

        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            update_operation(
                operation_id, status="succeeded", phase="completed",
                finished_at=now(), last_heartbeat=now(), exit_code=0,
                output=operation_output(output), error=operation_output(error), connection=db,
            )
            update_instance(instance_id, "planned", vmid=vmid, node=target_node, connection=db)
            emit(operation_id, "terraform.plan.completed", "succeeded", connection=db)
            db.execute("COMMIT")
    except OperationFencedError:
        raise
    except Exception as error:
        if isinstance(error, GuestIdentityError):
            current = instance_identity(instance_id)
            vmid = vmid or current.get("vmid")
            target_node = target_node or current.get("node")
            # El intento de reconciliación queda terminal para que una nueva
            # petición pueda volver a seleccionar la operación fuente incierta.
            status = "failed" if command == "reconcile" else "uncertain"
            update_operation(
                operation_id,
                status=status,
                phase="failed" if status == "failed" else "guest_identity_recovery_required",
                finished_at=now(),
                last_heartbeat=now(),
                exit_code=1,
                error=operation_output(str(error)),
            )
            update_instance(
                instance_id,
                "uncertain",
                vmid=vmid,
                power_state="unknown",
                node=target_node,
            )
            if command == "provision" and status != "uncertain":
                delete_secret(secret_path)
            emit(operation_id, "guest.identity.reconciliation_required", status, str(error))
            return
        physical = stack == "mng01" and phase in {"apply", "destroy", "reconcile"}
        power_uncertain = command == "power" and isinstance(error, ProxmoxError) and error.uncertain
        resource_protected = "PROTECTED_INSTANCE" in str(error)
        recovered = False
        if (
            physical
            and command == "destroy"
            and client is not None
            and vmid is not None
            and not deterministic_error(error)
        ):
            identity = instance_identity(instance_id)
            try:
                recovered = reconcile_physical(
                    client,
                    int(vmid),
                    command in {"apply", "provision"},
                    name=identity.get("name"),
                    node=target_node or identity.get("node"),
                    heartbeat_callback=lambda: heartbeat(worker_id, "running", operation_id, phase="recovery"),
                )
            except Exception:
                recovered = False
        if recovered:
            settle_physical_operation(
                operation_id,
                command,
                instance_id,
                int(vmid),
                target_node,
                source_payload,
                source_operation,
                output="La operación fue recuperada al confirmar el estado físico.",
                recovered=True,
            )
            return

        status = "uncertain" if (physical or power_uncertain) and not deterministic_error(error) else "failed"
        terminal_error = str(error)
        if command in {"plan", "provision"} and (phase == "plan" or status == "failed"):
            if not _release_static_allocation(instance_id, operation_id):
                status = "uncertain"
                terminal_error = "IPAM_RELEASE_PENDING"
        release_lease_for = (
            operation
            if command in {"plan", "provision", "apply"}
            and phase == "plan"
            and allocated_here
            and status == "failed"
            else None
        )
        preserve_instance = resource_protected or terminal_error in {
            "OPERATION_INSTANCE_MISMATCH",
            "OPERATION_INSTANCE_ALREADY_DELETED",
            "DESTROY_REQUIRES_STOPPED_INSTANCE",
            "NETWORK_POLICY_MISMATCH",
            "VM_PHYSICAL_OWNER_MISMATCH",
            "VMID_LEASE_OWNER_MISMATCH",
        }
        finalize_operation_failure(
            worker_id,
            operation_id,
            command,
            instance_id,
            vmid,
            target_node,
            status=status,
            error=operation_output(terminal_error),
            phase="recovery_required" if status == "uncertain" else "failed",
            power_uncertain=power_uncertain,
            preserve_instance=preserve_instance,
            release_lease_for=release_lease_for,
        )
        if command == "provision" and status != "uncertain":
            delete_secret(secret_path)
    finally:
        _PROXMOX_CREDENTIALS.reset(credential_scope)
        heartbeat(worker_id, "idle")


def run(worker_id: str, *, iterations: int | None = None) -> None:
    poll_interval = max(0.1, float(os.getenv("IACP_WORKER_POLL_SECONDS", "0.5")))
    backoff = Backoff(poll_interval)
    retry_until_ready(worker_id, "inicialización", initialize, backoff)
    retry_until_ready(worker_id, "heartbeat inicial", lambda: heartbeat(worker_id, "starting"), backoff)
    retry_until_ready(worker_id, "recuperación inicial", recover_stale_operations, backoff)
    recovery_scan_interval = max(5.0, float(os.getenv("IACP_RECOVERY_SCAN_SECONDS", "15")))
    last_recovery_scan = time.monotonic()
    idle_heartbeat_interval = max(
        1.0, float(os.getenv("IACP_WORKER_IDLE_HEARTBEAT_SECONDS", "15"))
    )
    last_idle_heartbeat = 0.0
    completed = 0
    while (iterations is None or completed < iterations) and not STOP.requested:
        completed += 1
        stage = "claim"
        try:
            if time.monotonic() - last_recovery_scan >= recovery_scan_interval:
                stage = "recuperación"
                recover_stale_operations()
                close_resolved_reconcile_attempts()
                last_recovery_scan = time.monotonic()
            stage = "claim"
            operation = claim(worker_id)
            if operation:
                stage = "ejecución"
                with operation_execution_lock(operation["operation_id"]):
                    token = _FENCE.set((worker_id, operation["operation_id"]))
                    try:
                        # Valida que la recuperación no haya ganado la carrera
                        # entre claim y adquisición del lock compartido.
                        heartbeat(worker_id, "running", operation["operation_id"], phase="claimed")
                        execute(worker_id, operation)
                    finally:
                        _FENCE.reset(token)
                last_idle_heartbeat = time.monotonic()
            else:
                now_monotonic = time.monotonic()
                if now_monotonic - last_idle_heartbeat >= idle_heartbeat_interval:
                    stage = "heartbeat"
                    heartbeat(worker_id, "idle")
                    last_idle_heartbeat = now_monotonic
                time.sleep(poll_interval)
            backoff.success()
        except Exception as error:  # noqa: BLE001 - se contiene y se reintenta
            delay = backoff.failure()
            report(worker_id, stage, error, delay)
            time.sleep(delay)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    worker_id = parser.parse_args().worker_id
    identify_process(worker_id)
    STOP.install(worker_id)
    run(worker_id)
