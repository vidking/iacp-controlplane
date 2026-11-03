import argparse
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone

from api.catalog import cluster_nodes, load_catalog
from api.db import database, initialize
from api.secrets import delete_secret, read_secret
from api.protection import is_protected_vmid
from api.targets import EnvironmentError as TargetEnvironmentError
from api.targets import resolve_environment, terraform_directory, wasabi_enabled
from worker.allocation import allocate, release
from worker.golden import observe, validate_pool, variables as golden_variables
from worker.heartbeat import heartbeat
from worker.proxmox import ProxmoxClient, ProxmoxError, load_environment


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


def _recovery_operation_exists(db, source_operation_id: str) -> bool:
    return db.execute(
        """
        SELECT 1 FROM operations
        WHERE command='reconcile' AND payload_json LIKE ?
        LIMIT 1
        """,
        (f"%{source_operation_id}%",),
    ).fetchone() is not None


def recover_stale_operations() -> int:
    """Turn abandoned worker claims into auditable reconciliation work.

    A worker crash cannot safely replay a mutation. The source operation is
    closed as uncertain and a single reconcile operation is queued so the
    next healthy worker verifies the physical state before settling it.
    """

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_operation_seconds())).isoformat()
    recovered = 0
    with database() as db:
        db.execute("BEGIN")
        db.execute("SELECT pg_advisory_xact_lock(104, 1)")
        rows = db.execute(
            f"""
            SELECT * FROM operations
            WHERE status IN ({','.join('?' for _ in ACTIVE_WORK_OPERATION_STATUSES)})
              AND (last_heartbeat IS NULL OR last_heartbeat < ?)
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            """,
            (*ACTIVE_WORK_OPERATION_STATUSES, cutoff),
        ).fetchall()
        for row in rows:
            operation = dict(row)
            operation_id = operation["operation_id"]
            command = operation.get("command") or "unknown"
            timestamp = now()
            terminal_status = "failed" if command == "plan" else "uncertain"
            detail = "El worker dejó de enviar heartbeat; se requiere reconciliación física."
            db.execute(
                """
                UPDATE operations
                SET status=?, phase=?, finished_at=?, last_heartbeat=?, exit_code=?, error=?
                WHERE operation_id=? AND status IN ('running', 'planning', 'applying', 'destroying', 'powering')
                """,
                (
                    terminal_status,
                    "recovery_required" if terminal_status == "uncertain" else "worker_lost",
                    timestamp,
                    timestamp,
                    1,
                    "WORKER_HEARTBEAT_STALE",
                    operation_id,
                ),
            )
            instance_id = operation.get("instance_id")
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

            if command == "plan" and operation.get("vmid") is not None:
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
                    "environment": operation.get("environment") or "lab",
                },
                sort_keys=True,
            )
            recovery_id = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO operations(
                  operation_id, command, action, status, payload_json, instance_id,
                  plan_id, environment, created_at, phase
                ) VALUES (?, 'reconcile', 'reconcile', 'pending', ?, ?, ?, ?, ?, 'recovery')
                """,
                (
                    recovery_id,
                    payload,
                    instance_id,
                    operation.get("plan_id"),
                    operation.get("environment") or "lab",
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
        db.execute("BEGIN")
        db.execute("SELECT pg_advisory_xact_lock(104, 2)")
        max_concurrent = int(os.getenv("IACP_MAX_CONCURRENT", "1"))
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
            "SELECT * FROM operations WHERE status = 'pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
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


def emit(operation_id: str, kind: str, status: str, detail: str = "") -> None:
    with database() as db:
        db.execute(
            "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, ?, ?, ?, ?)",
            (operation_id, kind, status, now(), detail),
        )


def update_operation(operation_id: str, **values) -> None:
    if not values:
        return
    assignments = ", ".join(f"{name}=?" for name in values)
    with database() as db:
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
) -> None:
    if not instance_id:
        return
    with database() as db:
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


def data_root() -> str:
    return os.getenv("IACP_WORKDATA_DIR", "/var/lib/iacp-controlplane")


def source_environment_directory(environment: str) -> str:
    return terraform_directory(environment)


def select_target_node(client: ProxmoxClient, environment: str = "lab") -> str:
    """Choose an online node from the server-side catalog for this environment."""

    configured = cluster_nodes(environment)
    node_rows = client.nodes()
    online = {
        str(row.get("node"))
        for row in node_rows
        if row.get("node") in configured and row.get("status", "online") == "online"
    }
    storage_id = load_catalog(environment).get("storage")
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
        raise RuntimeError("TARGET_NODE_UNAVAILABLE")

    counts = {node: 0 for node in candidates}
    for item in client.inventory():
        node = item.get("node")
        if node in counts:
            counts[node] += 1
    return min(candidates, key=lambda node: (counts[node], configured.index(node)))


def create_workspace(
    operation_id: str,
    source_path: str,
    environment: str,
) -> tuple[str, str]:
    root = os.path.join(data_root(), "plans", operation_id, "terraform")
    if os.path.exists(root):
        raise RuntimeError(f"el workspace ya existe: {root}")
    os.makedirs(os.path.dirname(root), exist_ok=True)
    ignore = shutil.ignore_patterns(
        ".terraform",
        "*.tfstate",
        "*.tfstate.*",
        "generated.auto.tfvars.json",
        "plan.tfplan",
    )
    shutil.copytree(source_path, root, ignore=ignore)
    relative_directory = source_environment_directory(environment)
    workspace = os.path.join(root, relative_directory)
    if not os.path.isdir(workspace):
        raise RuntimeError("ENVIRONMENT_CONFIGURATION_INCOMPLETE")
    # The image build initializes the certified provider once. Reuse that
    # immutable provider directory instead of downloading it for every
    # operation or copying tens of megabytes into each workspace.
    source_providers = os.path.join(
        source_path,
        relative_directory,
        ".terraform",
        "providers",
    )
    target_providers = os.path.join(workspace, ".terraform", "providers")
    if os.path.isdir(source_providers) and not os.path.exists(target_providers):
        os.makedirs(os.path.dirname(target_providers), exist_ok=True)
        os.symlink(source_providers, target_providers)
    return root, workspace


def _terraform_backend_configuration(environment: str, instance_id: str | None) -> dict | None:
    stage = os.getenv("IACP_RUNTIME_STAGE", "develop").strip().lower()
    try:
        remote_enabled = wasabi_enabled()
    except TargetEnvironmentError as error:
        raise RuntimeError("WASABI_CONFIGURATION_INVALID") from error
    if not remote_enabled:
        if environment != "lab":
            raise RuntimeError("REMOTE_STATE_REQUIRED_FOR_PRODUCTION")
        return None
    if stage != "production" or not instance_id:
        raise RuntimeError("TERRAFORM_BACKEND_CONFIGURATION_INVALID")
    bucket = os.getenv("IACP_TFSTATE_BUCKET", "").strip()
    prefix = os.getenv("IACP_TFSTATE_PREFIX", "").strip().strip("/")
    endpoint = os.getenv("IACP_TFSTATE_ENDPOINT", "").strip()
    region = os.getenv("IACP_TFSTATE_REGION", "").strip()
    if (
        not bucket
        or not endpoint.startswith("https://")
        or not region
        or prefix != "iacp/prod-runtime-v1"
        or not os.getenv("IACP_WASABI_ACCESS_KEY_ID", "").strip()
        or not os.getenv("IACP_WASABI_SECRET_ACCESS_KEY", "").strip()
    ):
        raise RuntimeError("TERRAFORM_BACKEND_CONFIGURATION_INVALID")
    return {
        "bucket": bucket,
        "key": f"{prefix}/instances/{instance_id}/terraform.tfstate",
        "region": region,
        "endpoints": {"s3": endpoint},
        "use_path_style": True,
        "skip_credentials_validation": True,
        "skip_region_validation": True,
        "skip_requesting_account_id": True,
        "skip_metadata_api_check": True,
        "skip_s3_checksum": True,
        "use_lockfile": True,
    }


def configure_state_backend(workspace: str, environment: str, instance_id: str | None) -> str | None:
    config = _terraform_backend_configuration(environment, instance_id)
    if config is None:
        return None
    config_path = os.path.join(workspace, ".iacp-backend.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, sort_keys=True)
        handle.write("\n")
    os.chmod(config_path, 0o600)
    return config_path


def write_variables(workspace: str, values: dict) -> str:
    path = os.path.join(workspace, "generated.auto.tfvars.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


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
    environment = os.environ.copy()
    if wasabi_enabled():
        environment["AWS_ACCESS_KEY_ID"] = os.getenv("IACP_WASABI_ACCESS_KEY_ID", "")
        environment["AWS_SECRET_ACCESS_KEY"] = os.getenv("IACP_WASABI_SECRET_ACCESS_KEY", "")
    else:
        environment.pop("AWS_ACCESS_KEY_ID", None)
        environment.pop("AWS_SECRET_ACCESS_KEY", None)
    plugin_cache = os.getenv("IACP_TERRAFORM_PLUGIN_CACHE_DIR", "").strip()
    if plugin_cache:
        environment["TF_PLUGIN_CACHE_DIR"] = plugin_cache
    password = read_secret(secret_path)
    if password is not None:
        environment["TF_VAR_root_password"] = password
    elif any(len(args) > 1 and args[0] == "terraform" and args[1] == "destroy" for args in commands):
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
            process = subprocess.Popen(
                args,
                cwd=workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            while process.poll() is None:
                if time.monotonic() > deadline:
                    process.kill()
                    process.wait(timeout=10)
                    raise TimeoutError("Terraform excedió el tiempo configurado")
                heartbeat(worker_id, "running", operation_id, phase=phase)
                time.sleep(1)
            stdout, stderr = process.communicate()
            stdout = stdout or ""
            stderr = stderr or ""
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


def terraform_commands(
    command: str,
    plan_path: str,
    backend_config_path: str | None = None,
) -> list[list[str]]:
    init = [
        "terraform",
        "init",
        "-input=false",
        "-no-color",
        "-lockfile=readonly",
    ]
    if backend_config_path:
        init.extend(["-reconfigure", f"-backend-config={backend_config_path}"])
    else:
        init.append("-backend=false")
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
) -> dict | None:
    """Read a VM record with a bounded retry window for transient transport errors."""

    attempts = max(1, int(os.getenv("IACP_RECONCILE_ATTEMPTS", "6")))
    for attempt in range(attempts):
        if heartbeat_callback is not None:
            heartbeat_callback()
        try:
            return next(
                (
                    dict(item)
                    for item in client.inventory(uncertain=True)
                    if int(item.get("vmid", -1)) == int(vmid)
                ),
                None,
            )
        except ProxmoxError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(8.0, 0.5 * (2**attempt)))
    return None


def physical_presence(
    client: ProxmoxClient,
    vmid: int,
    expected: bool,
    *,
    name: str | None = None,
    node: str | None = None,
    heartbeat_callback=None,
) -> bool:
    record = physical_record(client, vmid, heartbeat_callback=heartbeat_callback)
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
    if name and observed_name and observed_name != name:
        return False
    if node and observed_node and observed_node != node:
        return False
    return True


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
        if physical_presence(
            client,
            vmid,
            expected,
            name=name,
            node=node,
            heartbeat_callback=heartbeat_callback,
        ):
            return True
        time.sleep(min(8.0, float(os.getenv("IACP_RECONCILE_BACKOFF", "2"))))
    return False


def instance_identity(instance_id: str | None) -> dict:
    if not instance_id:
        return {}
    with database() as db:
        row = db.execute(
            "SELECT instance_id, vmid, node, name, status, power_state FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
    return dict(row) if row else {}


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
    update_operation(
        operation_id,
        status="succeeded",
        phase="completed",
        finished_at=now(),
        last_heartbeat=now(),
        exit_code=0,
        output=operation_output(output),
        error=operation_output(""),
    )
    if command == "destroy":
        update_instance(instance_id, "deleted", vmid=vmid, power_state="unknown")
        if vmid is not None:
            release(int(vmid))
        delete_secret(source_operation.get("secret_path"))
    elif command in {"apply", "provision"}:
        started = bool(source_payload.get("spec", {}).get("started", True))
        update_instance(
            instance_id,
            "ready",
            vmid=vmid,
            power_state="running" if started else "stopped",
            node=node,
        )
        if command == "provision":
            delete_secret(source_operation.get("secret_path"))
    emit(
        operation_id,
        f"terraform.{command}.completed",
        "succeeded",
        "physical state reconciled" if recovered else "",
    )
    return True


def close_source_after_reconciliation(
    source_operation: dict,
    instance_id: str | None,
    vmid: int | None,
    node: str | None,
    source_payload: dict,
    *,
    succeeded: bool,
    detail: str,
) -> None:
    source_id = source_operation["operation_id"]
    if succeeded:
        settle_physical_operation(
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
        terminal_status = "failed"
        terminal_error = detail
        update_operation(
            source_id,
            status=terminal_status,
            finished_at=now(),
            last_heartbeat=now(),
            exit_code=1,
            output="",
            error=terminal_error,
        )
        update_instance(instance_id, terminal_status, vmid=vmid, node=node)
        if vmid is not None and source_operation["command"] in {"apply", "provision"}:
            # Reconciliation proved that the physical VM is absent. Release
            # the lease so a later request can reuse the lowest free VMID.
            release(int(vmid))
        emit(source_id, "operation.reconciled", terminal_status, terminal_error)


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

    identity = instance_identity(instance_id or source.get("instance_id"))
    instance_id = identity.get("instance_id") or source.get("instance_id")
    vmid = identity.get("vmid") or source.get("vmid")
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

    if vmid is None:
        close_source_after_reconciliation(
            source, instance_id, None, node, source_payload,
            succeeded=source_command == "destroy",
            detail="No había VM física para reconciliar.",
        )
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

    if source_command == "power":
        action = source_payload.get("power_action")
        if action not in {"start", "shutdown", "reboot"}:
            raise RuntimeError("POWER_ACTION_INVALID")
        observed = client.power_state(node, int(vmid), name)
        desired = "running" if action == "start" else "stopped"
        if action in {"start", "shutdown"} and observed.get("power_state") == desired:
            result = {"action": action, "already": True, **observed}
        elif action == "reboot":
            # A worker crash can happen after qmreboot was accepted but before
            # its task result was persisted. Running alone cannot prove that a
            # reboot happened, so never issue a second reboot automatically.
            raise ProxmoxError("POWER_REBOOT_REQUIRES_REVIEW", uncertain=True)
        else:
            result = client.power(node, int(vmid), name, action)
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
        close_source_after_reconciliation(
            source, instance_id, int(vmid), node, source_payload,
            succeeded=True,
            detail="La presencia física coincide con la operación solicitada.",
        )
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
        raise ProxmoxError("RECONCILIATION_REQUIRED", uncertain=True)

    close_source_after_reconciliation(
        source, instance_id, int(vmid), node, source_payload,
        succeeded=False,
        detail="RECONCILIATION_PHYSICAL_RESOURCE_ABSENT",
    )
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
            "SELECT instance_id, vmid, node, name, status FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
    if row is None or row["vmid"] is None:
        raise RuntimeError("POWER_INSTANCE_NOT_PROVISIONED")
    if is_protected_vmid(row["vmid"]):
        raise RuntimeError("PROTECTED_INSTANCE")
    if row["status"] == "deleted":
        raise RuntimeError("POWER_INSTANCE_DELETED")
    if action not in {"start", "shutdown", "reboot"}:
        raise RuntimeError("POWER_ACTION_INVALID")

    vmid = int(row["vmid"])
    node = row["node"] or "mng01"
    update_operation(operation_id, vmid=vmid)
    update_instance(instance_id, "powering", vmid=vmid, power_state="unknown")
    emit(operation_id, f"power.{action}.started", "running")
    setter = getattr(client, "set_heartbeat", None)
    if setter is not None:
        setter(lambda: heartbeat(worker_id, "running", operation_id, phase=f"power_{action}"))
    heartbeat(worker_id, "running", operation_id, phase=f"power_{action}")
    result = client.power(node, vmid, row["name"], action)
    power_state = result.get("power_state")
    update_operation(
        operation_id,
        status="succeeded",
        finished_at=now(),
        last_heartbeat=now(),
        exit_code=0,
        output=json.dumps(result, sort_keys=True),
        error="",
    )
    update_instance(instance_id, "ready", vmid=vmid, power_state=power_state)
    emit(operation_id, f"power.{action}.completed", "succeeded")


def execute(worker_id: str, operation: dict) -> None:
    operation_id = operation["operation_id"]
    command = "unknown"
    phase = "plan"
    environment = operation.get("environment") or "lab"
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
    network_model: str | None = None
    backend_config_path: str | None = None
    try:
        payload = json.loads(operation["payload_json"])
        command = payload["command"]
        source_payload = payload
        environment = resolve_environment(payload.get("environment") or environment)
        instance_id = payload.get("instance_id") or instance_id

        if command in {"plan", "provision", "apply", "destroy"}:
            _terraform_backend_configuration(environment, instance_id)

        if command == "power":
            endpoint, token = load_environment(environment)
            client = ProxmoxClient(endpoint, token, environment=environment)
            execute_power(worker_id, operation, client)
            return

        if command == "reconcile":
            phase = "reconcile"
            endpoint, token = load_environment(environment)
            client = ProxmoxClient(endpoint, token, environment=environment)
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
            environment = resolve_environment(source_operation.get("environment") or environment)
            instance_id = source_operation.get("instance_id") or instance_id
            target_node = source_operation.get("target_node") or target_node
        requested_started = bool(source_payload.get("spec", {}).get("started", True))

        source_path = os.getenv("IACP_TERRAFORM_PATH", "/workspace/terraform")
        endpoint, token = load_environment(environment)
        client = ProxmoxClient(endpoint, token, environment=environment)

        secret_path = secret_path_for_command(command, source_operation, operation)

        if command in {"plan", "provision"}:
            spec = payload.get("spec")
            if not spec:
                raise RuntimeError(f"{command} requiere spec")
            pool_id = validate_pool(client, environment)
            emit(operation_id, "proxmox.pool.validated", "ok", pool_id)
            golden = observe(spec["image"], client, environment)
            emit(operation_id, "golden.observed", "ok", spec["image"])
            network_model = golden.get("network_model") or "virtio"
            requested_started = bool(spec.get("started", True))
            target_node = target_node or select_target_node(client, environment)
            vmid = allocate(operation_id, client, node=target_node)
            allocated_here = True
            values = golden_variables(
                spec,
                vmid,
                golden,
                environment,
                target_node=target_node,
                allowed_nodes=cluster_nodes(environment),
                has_root_password=read_secret(secret_path) is not None,
                pool_id=pool_id,
            )
            update_instance(instance_id, "planning", vmid=vmid, node=target_node)
            emit(operation_id, "vmid.reserved", "held", str(vmid))
            workspace_root, workspace = create_workspace(operation_id, source_path, environment)
            backend_config_path = configure_state_backend(workspace, environment, instance_id)
            write_variables(workspace, values)
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
            workspace = os.path.join(workspace_root, source_environment_directory(environment))
            if not os.path.isdir(workspace):
                raise RuntimeError(f"no existe el workspace del plan {source_operation_id}")
            existing_backend_config = os.path.join(workspace, ".iacp-backend.json")
            has_remote_backend = os.path.isfile(existing_backend_config)
            remote_enabled = wasabi_enabled()
            if remote_enabled and not has_remote_backend:
                raise RuntimeError("TERRAFORM_BACKEND_CONFIGURATION_INVALID")
            if not remote_enabled and has_remote_backend:
                raise RuntimeError("REMOTE_STATE_DISABLED_WORKSPACE")
            backend_config_path = existing_backend_config if remote_enabled else None
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
            if is_protected_vmid(vmid):
                raise RuntimeError("PROTECTED_INSTANCE")
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
                terraform_commands("plan", plan_path, backend_config_path),
                secret_path=secret_path,
                phase=phase,
            )
            if return_code == 0:
                emit(operation_id, "terraform.plan.completed", "succeeded")
                phase = "apply"
                update_operation(operation_id, phase=phase)
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
            emit(operation_id, f"terraform.{command}.started", "running")
            heartbeat(worker_id, "running", operation_id, phase=phase)
            return_code, output, error = run_commands(
                worker_id,
                operation_id,
                workspace,
                terraform_commands(command, plan_path, backend_config_path),
                secret_path=secret_path,
                phase=phase,
            )
        if return_code != 0:
            physical = client is not None and phase in {"apply", "destroy"}
            failure_text = f"{output}\n{error}"
            recovered = False
            if physical and client is not None and vmid is not None and not deterministic_error(failure_text):
                emit(operation_id, "recovery.started", "running", "physical operation")
                identity = instance_identity(instance_id)
                expected = command in {"apply", "provision"}
                # First observe the physical state. A lost Terraform response
                # must never cause a second mutation when the requested state
                # is already present.
                for retry in range(2):
                    if return_code == 0:
                        break
                    if not retryable_command_error(failure_text):
                        break
                    try:
                        recovered = physical_presence(
                            client,
                            int(vmid),
                            expected,
                            name=identity.get("name"),
                            node=target_node or identity.get("node"),
                            heartbeat_callback=lambda: heartbeat(
                                worker_id, "running", operation_id, phase="recovery"
                            ),
                        )
                    except Exception:
                        recovered = False
                    if recovered:
                        break
                    time.sleep(min(15.0, 2 * (2**retry)))
                    retry_code, retry_output, retry_error = run_commands(
                        worker_id,
                        operation_id,
                        workspace,
                        terraform_commands(command, plan_path, backend_config_path)
                        if command != "provision"
                        else [["terraform", "apply", "-input=false", "-no-color", plan_path]],
                        secret_path=secret_path,
                    )
                    output += retry_output
                    error += retry_error
                    return_code = retry_code
                    failure_text = f"{output}\n{error}"
                if return_code != 0 and not recovered:
                    recovered = reconcile_physical(
                        client,
                        int(vmid),
                        command in {"apply", "provision"},
                        name=identity.get("name"),
                        node=target_node or identity.get("node"),
                        heartbeat_callback=lambda: heartbeat(worker_id, "running", operation_id, phase="recovery"),
                    )
                if recovered:
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
            terminal_error = error
            update_operation(
                operation_id,
                status=status,
                phase="recovery_required" if status == "uncertain" else "failed",
                finished_at=now(),
                last_heartbeat=now(),
                exit_code=return_code,
                output=operation_output(output),
                error=operation_output(terminal_error),
            )
            update_instance(instance_id, status, node=target_node)
            emit(operation_id, f"terraform.{command}.completed", status, str(terminal_error)[-2000:])
            if command in {"plan", "provision", "apply"} and (phase == "plan" or status == "failed"):
                if vmid is not None and allocated_here:
                    release(int(vmid))
            if command == "provision":
                delete_secret(secret_path)
            return

        if vmid is not None and client is not None:
            expected = command in {"apply", "provision"}
            identity = instance_identity(instance_id)
            if not reconcile_physical(
                client,
                int(vmid),
                expected,
                name=identity.get("name"),
                node=target_node or identity.get("node"),
                heartbeat_callback=lambda: heartbeat(worker_id, "running", operation_id, phase=phase),
            ):
                raise RuntimeError(
                    f"reconciliacion fisica fallida: VMID {vmid} no coincide con {expected}"
                )

        update_operation(
            operation_id,
            status="succeeded",
            phase="completed",
            finished_at=now(),
            last_heartbeat=now(),
            exit_code=0,
            output=operation_output(output),
            error=operation_output(error),
        )
        if command == "plan":
            update_instance(instance_id, "planned", vmid=vmid, node=target_node)
        elif command in {"apply", "provision"}:
            started = requested_started if requested_started is not None else bool(source_payload.get("spec", {}).get("started", True))
            update_instance(
                instance_id,
                "ready",
                vmid=vmid,
                power_state="running" if started else "stopped",
                node=target_node,
            )
            if command == "provision":
                delete_secret(secret_path)
        else:
            update_instance(instance_id, "deleted", vmid=vmid, power_state="unknown")
            if vmid is not None:
                release(int(vmid))
            delete_secret(source_operation.get("secret_path"))
        emit(operation_id, f"terraform.{command}.completed", "succeeded")
    except Exception as error:
        physical = client is not None and phase in {"apply", "destroy", "reconcile"}
        power_uncertain = command == "power" and isinstance(error, ProxmoxError) and error.uncertain
        recovered = False
        if (
            physical
            and command != "reconcile"
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
        if command in {"plan", "provision"} and phase == "plan" and allocated_here and vmid is not None:
            release(int(vmid))
        terminal_error = str(error)
        if command == "provision":
            delete_secret(secret_path)
        update_operation(
            operation_id,
            status=status,
            phase="recovery_required" if status == "uncertain" else "failed",
            finished_at=now(),
            last_heartbeat=now(),
            error=operation_output(terminal_error),
        )
        if command == "power" and not power_uncertain:
            update_instance(instance_id, "ready", vmid=vmid, node=target_node)
        else:
            update_instance(
                instance_id,
                status,
                vmid=vmid,
                power_state="unknown" if command == "power" else None,
                node=target_node,
            )
        emit(operation_id, f"terraform.{command}.failed", status, terminal_error)
    finally:
        heartbeat(worker_id, "idle")


def run(worker_id: str) -> None:
    initialize()
    heartbeat(worker_id, "starting")
    recover_stale_operations()
    recovery_scan_interval = max(5.0, float(os.getenv("IACP_RECOVERY_SCAN_SECONDS", "15")))
    last_recovery_scan = time.monotonic()
    idle_heartbeat_interval = max(
        1.0, float(os.getenv("IACP_WORKER_IDLE_HEARTBEAT_SECONDS", "15"))
    )
    poll_interval = max(0.1, float(os.getenv("IACP_WORKER_POLL_SECONDS", "0.5")))
    last_idle_heartbeat = 0.0
    while True:
        if time.monotonic() - last_recovery_scan >= recovery_scan_interval:
            recover_stale_operations()
            last_recovery_scan = time.monotonic()
        operation = claim(worker_id)
        if operation:
            execute(worker_id, operation)
            last_idle_heartbeat = time.monotonic()
        else:
            now_monotonic = time.monotonic()
            if now_monotonic - last_idle_heartbeat >= idle_heartbeat_interval:
                heartbeat(worker_id, "idle")
                last_idle_heartbeat = now_monotonic
            time.sleep(poll_interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    run(parser.parse_args().worker_id)
