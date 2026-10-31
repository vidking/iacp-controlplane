import argparse
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from api.db import database
from worker.allocation import allocate, release
from worker.golden import observe, variables as golden_variables
from worker.heartbeat import heartbeat
from worker.proxmox import ProxmoxClient, ProxmoxError, load_environment
from worker.secrets import decrypt_access
from worker.sanitize import sanitize_text


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def claim(worker_id: str):
    with database() as db:
        db.execute("BEGIN")
        active = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE status IN ('running', 'planning', 'applying', 'destroying', 'powering')"
        ).fetchone()["count"]
        if active >= int(os.getenv("IACP_MAX_CONCURRENT", "2")):
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
            "UPDATE operations SET status='running', worker_id=?, started_at=?, last_heartbeat=? WHERE operation_id=?",
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
            (operation_id, kind, status, now(), sanitize_text(detail, limit=4000)),
        )


def update_operation(operation_id: str, **values) -> None:
    if not values:
        return
    if "output" in values:
        values["output"] = sanitize_text(values["output"])
    if "error" in values:
        values["error"] = sanitize_text(values["error"])
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
) -> None:
    if not instance_id:
        return
    with database() as db:
        if vmid is None:
            db.execute(
                "UPDATE instances SET status=?, updated_at=? WHERE instance_id=?",
                (status, now(), instance_id),
            )
        else:
            db.execute(
                "UPDATE instances SET status=?, vmid=?, updated_at=? WHERE instance_id=?",
                (status, vmid, now(), instance_id),
            )
        if power_state is not None:
            db.execute(
                "UPDATE instances SET power_state=?, updated_at=? WHERE instance_id=?",
                (power_state, now(), instance_id),
            )


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
    return os.getenv("IACP_DATA_ROOT", "/var/lib/iacp")


def source_stack_directory(stack: str) -> str:
    if stack == "local":
        return os.getenv("IACP_LOCAL_TERRAFORM_DIR", "fixtures/local")
    return os.getenv("IACP_TERRAFORM_DIR", "environments/mng01")


def create_workspace(operation_id: str, source_path: str, stack: str, instance_id: str | None) -> tuple[str, str]:
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
    if stack == "mng01" and instance_id:
        write_backend(root, instance_id)
    return root, workspace


def write_backend(workspace_root: str, instance_id: str) -> str:
    bucket = os.getenv("IACP_WASABI_BUCKET", "").strip()
    region = os.getenv("IACP_WASABI_REGION", "").strip()
    endpoint = os.getenv("IACP_WASABI_ENDPOINT", "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or not re.fullmatch(r"[a-z0-9-]+", region)
        or parsed.scheme != "https"
        or not (parsed.hostname or "").endswith(".wasabisys.com")
    ):
        raise RuntimeError("WASABI_BACKEND_CONFIGURATION_INVALID")
    path = os.path.join(workspace_root, "backend.hcl")
    values = [
        f'bucket = "{bucket}"',
        f'key = "iacp/lab/instances/{instance_id}/terraform.tfstate"',
        f'region = "{region}"',
        f'endpoints = {{ s3 = "{endpoint}" }}',
        "use_lockfile = true",
        "encrypt = true",
        "use_path_style = true",
        "skip_credentials_validation = true",
        "skip_region_validation = true",
        "skip_requesting_account_id = true",
        "skip_metadata_api_check = true",
    ]
    Path(path).write_text("\n".join(values) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


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


def execution_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for variable, path_variable in (
        ("AWS_ACCESS_KEY_ID", "IACP_WASABI_ACCESS_KEY_FILE"),
        ("AWS_SECRET_ACCESS_KEY", "IACP_WASABI_SECRET_KEY_FILE"),
        ("AWS_SESSION_TOKEN", "IACP_WASABI_SESSION_TOKEN_FILE"),
    ):
        path = os.getenv(path_variable, "")
        if path and os.path.exists(path):
            value = Path(path).read_text(encoding="utf-8").strip()
            if value:
                environment[variable] = value
    return environment


def run_commands(worker_id: str, operation_id: str, workspace: str, commands: list[list[str]]) -> tuple[int, str, str]:
    deadline = time.monotonic() + int(os.getenv("IACP_TERRAFORM_TIMEOUT", "1800"))
    output: list[str] = []
    errors: list[str] = []
    for args in commands:
        process = subprocess.Popen(
            args,
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=execution_environment(),
        )
        while process.poll() is None:
            if time.monotonic() > deadline:
                process.kill()
                process.wait(timeout=10)
                raise TimeoutError("Terraform excedió el tiempo configurado")
            heartbeat(worker_id, "running", operation_id)
            time.sleep(1)
        stdout, stderr = process.communicate()
        output.append(stdout or "")
        errors.append(stderr or "")
        if process.returncode != 0:
            return process.returncode, "".join(output), "".join(errors)
    return 0, "".join(output), "".join(errors)


def terraform_commands(command: str, plan_path: str, backend_path: str | None = None) -> list[list[str]]:
    init = ["terraform", "init", "-input=false", "-no-color"]
    if backend_path:
        init.extend(["-backend-config", backend_path])
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
    raise RuntimeError(f"orden Terraform no soportada: {command}")


def physical_presence(client: ProxmoxClient, vmid: int, expected: bool) -> bool:
    present = any(int(item.get("vmid", -1)) == vmid for item in client.inventory())
    return present is expected


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
    if row["status"] == "deleted":
        raise RuntimeError("POWER_INSTANCE_DELETED")
    if action not in {"start", "shutdown", "reboot"}:
        raise RuntimeError("POWER_ACTION_INVALID")

    vmid = int(row["vmid"])
    node = row["node"] or "mng01"
    update_operation(operation_id, vmid=vmid)
    update_instance(instance_id, "powering", vmid=vmid, power_state="unknown")
    emit(operation_id, f"power.{action}.started", "running")
    heartbeat(worker_id, "running", operation_id)
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
    stack = operation.get("stack") or "mng01"
    instance_id = operation.get("instance_id")
    vmid: int | None = operation.get("vmid")
    allocated_here = False
    client: ProxmoxClient | None = None
    try:
        payload = json.loads(operation["payload_json"])
        command = payload["command"]
        stack = payload.get("stack") or stack
        instance_id = payload.get("instance_id") or instance_id

        if command == "power":
            if stack != "mng01":
                raise RuntimeError("POWER_STACK_UNSUPPORTED")
            load_environment()
            client = ProxmoxClient()
            execute_power(worker_id, operation, client)
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
            instance_id = source_operation.get("instance_id") or instance_id

        source_path = os.getenv("IACP_TERRAFORM_PATH", "/workspace/terraform")
        if stack == "mng01":
            load_environment()
            client = ProxmoxClient()

        if command == "plan":
            spec = payload.get("spec")
            if not spec:
                raise RuntimeError("plan requiere spec")
            if stack == "mng01":
                golden = observe(spec["image"], client)
                emit(operation_id, "golden.observed", "ok", spec["image"])
                vmid = allocate(operation_id, client)
                allocated_here = True
                values = golden_variables(
                    spec,
                    vmid,
                    golden,
                    stack,
                )
                update_instance(instance_id, "planning", vmid=vmid)
                emit(operation_id, "vmid.reserved", "held", str(vmid))
            else:
                values = local_variables(spec)
                update_instance(instance_id, "planning")
            workspace_root, workspace = create_workspace(operation_id, source_path, stack, instance_id)
            write_variables(workspace, values)
            plan_path = os.path.join(data_root(), "plans", operation_id, "plan.tfplan")
            update_operation(
                operation_id,
                workspace_path=workspace_root,
                plan_path=plan_path,
                vmid=vmid,
            )
        else:
            workspace_root = source_operation.get("workspace_path") or os.path.join(
                data_root(), "plans", source_operation_id, "terraform"
            )
            workspace = os.path.join(workspace_root, source_stack_directory(stack))
            if not os.path.isdir(workspace):
                raise RuntimeError(f"no existe el workspace del plan {source_operation_id}")
            plan_path = source_operation.get("plan_path") or os.path.join(
                data_root(), "plans", source_operation_id, "plan.tfplan"
            )
            vmid = source_operation.get("vmid")
            if vmid is None and instance_id:
                with database() as db:
                    row = db.execute(
                        "SELECT vmid FROM instances WHERE instance_id=?", (instance_id,)
                    ).fetchone()
                vmid = row["vmid"] if row else None
            update_operation(operation_id, vmid=vmid, workspace_path=workspace_root, plan_path=plan_path)
            if command == "apply":
                update_instance(instance_id, "applying", vmid=vmid)
            else:
                update_instance(instance_id, "destroying", vmid=vmid)

        emit(operation_id, f"terraform.{command}.started", "running")
        heartbeat(worker_id, "running", operation_id)
        backend_path = os.path.join(workspace_root, "backend.hcl") if stack == "mng01" else None
        return_code, output, error = run_commands(
            worker_id,
            operation_id,
            workspace,
            terraform_commands(command, plan_path, backend_path),
        )
        if return_code != 0:
            physical = stack == "mng01" and command in {"apply", "destroy"}
            status = "uncertain" if physical else "failed"
            update_operation(
                operation_id,
                status=status,
                finished_at=now(),
                last_heartbeat=now(),
                exit_code=return_code,
                output=output,
                error=error,
            )
            update_instance(instance_id, status)
            emit(operation_id, f"terraform.{command}.completed", status, error[-2000:])
            return

        if stack == "mng01" and vmid is not None and client is not None:
            expected = command == "apply"
            if not physical_presence(client, int(vmid), expected):
                raise RuntimeError(
                    f"reconciliacion fisica fallida: VMID {vmid} no coincide con {expected}"
                )

        if command == "plan":
            update_operation(
                operation_id,
                status="succeeded",
                finished_at=now(),
                last_heartbeat=now(),
                exit_code=0,
                output=output,
                error=error,
            )
            update_instance(instance_id, "planned", vmid=vmid)
        elif command == "apply":
            if source_operation.get("access_ciphertext"):
                from worker.guest import initialize_access

                initialize_access(
                    client,
                    node="mng01",
                    vmid=int(vmid),
                    name=source_payload.get("spec", {}).get("name", ""),
                    password=access_secret(source_operation),
                )
                clear_access(source_operation_id)
            update_operation(
                operation_id,
                status="succeeded",
                finished_at=now(),
                last_heartbeat=now(),
                exit_code=0,
                output=output,
                error=error,
            )
            started = bool(source_payload.get("spec", {}).get("started", True))
            update_instance(
                instance_id,
                "ready",
                vmid=vmid,
                power_state="running" if started else "stopped",
            )
        else:
            update_operation(
                operation_id,
                status="succeeded",
                finished_at=now(),
                last_heartbeat=now(),
                exit_code=0,
                output=output,
                error=error,
            )
            update_instance(instance_id, "deleted", vmid=vmid, power_state="unknown")
            if vmid is not None:
                release(int(vmid))
        emit(operation_id, f"terraform.{command}.completed", "succeeded")
    except Exception as error:
        physical = stack == "mng01" and command in {"apply", "destroy"}
        power_uncertain = command == "power" and isinstance(error, ProxmoxError) and error.uncertain
        status = "uncertain" if physical or power_uncertain else "failed"
        if command == "plan" and allocated_here and vmid is not None:
            release(int(vmid))
        update_operation(
            operation_id,
            status=status,
            finished_at=now(),
            last_heartbeat=now(),
            error=sanitize_text(str(error)),
        )
        if command == "power" and not power_uncertain:
            update_instance(instance_id, "ready", vmid=vmid)
        else:
            update_instance(
                instance_id,
                status,
                vmid=vmid,
                power_state="unknown" if command == "power" else None,
            )
        emit(operation_id, f"terraform.{command}.failed", status, str(error))
    finally:
        heartbeat(worker_id, "idle")


def clear_access(operation_id: str) -> None:
    with database() as db:
        db.execute(
            "UPDATE operations SET access_ciphertext=NULL, access_expires_at=NULL WHERE operation_id=?",
            (operation_id,),
        )


def access_secret(source_operation: dict) -> str:
    expires_at = source_operation.get("access_expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires_at <= datetime.now(timezone.utc):
            clear_access(source_operation["operation_id"])
            raise RuntimeError("ROOT_ACCESS_SECRET_EXPIRED")
    return decrypt_access(source_operation["access_ciphertext"])


def run(worker_id: str) -> None:
    while True:
        operation = claim(worker_id)
        if operation:
            execute(worker_id, operation)
        else:
            heartbeat(worker_id, "idle")
            time.sleep(float(os.getenv("IACP_WORKER_POLL_SECONDS", "2")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    run(parser.parse_args().worker_id)
