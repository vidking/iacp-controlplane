import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

from api.db import database, initialize
from worker.heartbeat import heartbeat


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def claim(worker_id: str):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM operations WHERE status = 'pending' ORDER BY created_at LIMIT 1"
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


def emit(operation_id: str, kind: str, status: str, detail: str = ""):
    with database() as db:
        db.execute(
            "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, ?, ?, ?, ?)",
            (operation_id, kind, status, now(), detail),
        )


def execute(worker_id: str, operation: dict):
    operation_id = operation["operation_id"]
    command = "unknown"
    try:
        payload = json.loads(operation["payload_json"])
        source_path = os.getenv("IACP_TERRAFORM_PATH", "/workspace/terraform")
        terraform_dir = os.getenv("IACP_TERRAFORM_DIR", "environments/mng01")
        command = payload["command"]
        token_file = os.getenv("PROXMOX_VE_API_TOKEN_FILE", "")
        if token_file and os.path.isfile(token_file):
            with open(token_file, encoding="utf-8") as handle:
                os.environ["PROXMOX_VE_API_TOKEN"] = handle.read().strip()
        data_root = os.path.dirname(os.getenv("IACP_DB_PATH", "data/iacp.sqlite")) or "data"
        plan_root = os.path.join(data_root, "plans")
        os.makedirs(plan_root, exist_ok=True)
        plan_operation_id = payload.get("plan_operation_id")
        if command == "apply" and not plan_operation_id:
            raise RuntimeError("apply requiere plan_operation_id")
        if command == "apply":
            workspace_root = os.path.join(plan_root, plan_operation_id, "terraform")
            workspace = os.path.join(workspace_root, terraform_dir)
            if not os.path.isdir(workspace):
                raise RuntimeError(f"no existe el workspace del plan {plan_operation_id}")
        else:
            workspace_root = os.path.join(plan_root, operation_id, "terraform")
            if os.path.exists(workspace_root):
                shutil.rmtree(workspace_root)
            shutil.copytree(source_path, workspace_root)
            workspace = os.path.join(workspace_root, terraform_dir)
            if payload.get("variables"):
                with open(os.path.join(workspace, "generated.auto.tfvars.json"), "w", encoding="utf-8") as handle:
                    json.dump(payload["variables"], handle, indent=2)
        if not os.path.isdir(workspace):
            raise RuntimeError(f"no existe el workspace Terraform {workspace}")
        emit(operation_id, f"terraform.{command}.started", "running")
        heartbeat(worker_id, "running", operation_id)
        plan_file = os.path.join(plan_root, plan_operation_id or operation_id, "plan.tfplan")
        if command == "plan":
            commands = [["terraform", "init", "-input=false", "-no-color"], ["terraform", "plan", "-input=false", "-no-color", "-out", plan_file]]
        elif command == "apply":
            commands = [["terraform", "apply", "-input=false", "-no-color", plan_file]]
        else:
            commands = [["terraform", "init", "-input=false", "-no-color"], ["terraform", "destroy", "-input=false", "-auto-approve", "-no-color"]]
        output_parts: list[str] = []
        error_parts: list[str] = []
        deadline = time.monotonic() + int(os.getenv("IACP_TERRAFORM_TIMEOUT", "1800"))
        return_code = 0
        for args in commands:
            process = subprocess.Popen(args, cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            while process.poll() is None:
                if time.monotonic() > deadline:
                    process.kill()
                    raise TimeoutError("Terraform excedió el tiempo configurado")
                heartbeat(worker_id, "running", operation_id)
                time.sleep(5)
            stdout, stderr = process.communicate()
            output_parts.append(stdout)
            error_parts.append(stderr)
            return_code = process.returncode
            if return_code != 0:
                break
        status = "succeeded" if return_code == 0 else "failed"
        with database() as db:
            db.execute(
                "UPDATE operations SET status=?, finished_at=?, last_heartbeat=?, exit_code=?, output=?, error=? WHERE operation_id=?",
                (status, now(), now(), return_code, "".join(output_parts), "".join(error_parts), operation_id),
            )
        emit(operation_id, f"terraform.{command}.completed", status, "".join(error_parts)[-2000:])
    except Exception as error:
        with database() as db:
            db.execute(
                "UPDATE operations SET status='failed', finished_at=?, last_heartbeat=?, error=? WHERE operation_id=?",
                (now(), now(), str(error), operation_id),
            )
        emit(operation_id, f"terraform.{command}.failed", "failed", str(error))
    finally:
        heartbeat(worker_id, "idle")


def run(worker_id: str):
    initialize()
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

