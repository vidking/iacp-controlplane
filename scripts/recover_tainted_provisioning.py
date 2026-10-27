#!/usr/bin/env python3
"""Limpieza acotada de un aprovisionamiento KVM fallido con state tainted.

La herramienta no adopta el recurso como READY y no ejecuta un nuevo apply.
Solo acepta un UUID exacto cuyo VMID, nodo, nombre, state y ownership se
derivan de la asignación persistida. El plan Terraform debe contener una única
acción ``delete`` antes de que se permita el destroy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import sys
from uuid import UUID, uuid4
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from iacp.db import get_session_factory
from iacp.models import Instance, Job, Operation, OperationEvent, TerraformApply, TerraformAttempt, TerraformExecution
from iacp.services.instances import event
from iacp.terraform.config import Allocation, PlanError, SOURCE_REVISION
from iacp.terraform.executor import state_resource
from iacp.terraform.locks import ExecutionBusy, instance_lock
from iacp.terraform.deletion_executor import DeletionExecutor
from iacp.terraform.observation import proxmox_resource
from iacp.terraform.state import S3StateReader
from iacp.terraform.lifecycle import build_executor
from iacp.lifecycle.proxmox import NoRedirect, PowerError, ProxmoxPower, Target, granted


PROTECTED = {104, 106, 29000, 29001, 29010, 29011, 29016, 29017}


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("x", encoding="utf8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2, default=str)
        stream.write("\n")


def cancelled() -> bool:
    return False


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanError(message)


def context_for(instance, execution):
    allocation = Allocation.model_validate(execution.allocation)
    require(execution.vmid == allocation.vm_id, "RECOVERY_ALLOCATION_VM_MISMATCH")
    require(execution.vmid >= 30000 and execution.vmid not in PROTECTED, "RECOVERY_PROTECTED_VM")
    variables = allocation.variables(instance)
    require(variables["instance"]["vm_id"] == execution.vmid, "RECOVERY_VARIABLE_VM_MISMATCH")
    require(variables["instance"]["node_name"] == allocation.node_name, "RECOVERY_VARIABLE_NODE_MISMATCH")
    require(variables["instance"]["vm_name"] == instance.name, "RECOVERY_VARIABLE_NAME_MISMATCH")
    return allocation, {
        "instance_id": instance.id,
        # Workspace aislado para esta recuperación; la auditoría sigue ligada
        # al operation_id original mediante los eventos persistidos.
        "operation_id": str(uuid4()),
        "attempt": execution.attempt,
        "provisioning_plan_sha256": execution.plan_sha256,
        "state_key": execution.state_key,
        "allocation": execution.allocation,
        "variables": variables,
        "backend": allocation.backend.arguments(execution.state_key),
    }


def verify_database(session, instance_id: str, *, finalize=False):
    instance = session.get(Instance, instance_id, with_for_update=True, populate_existing=True)
    require(instance is not None, "RECOVERY_INSTANCE_NOT_FOUND")
    execution = session.get(TerraformExecution, instance_id, with_for_update=True, populate_existing=True)
    require(execution is not None, "RECOVERY_EXECUTION_NOT_FOUND")
    apply = session.scalar(select(TerraformApply).where(
        TerraformApply.instance_id == instance_id, TerraformApply.attempt == execution.attempt
    ).with_for_update())
    require(apply is not None, "RECOVERY_APPLY_NOT_FOUND")
    operation = session.get(Operation, execution.operation_id, with_for_update=True)
    job = session.get(Job, execution.job_id, with_for_update=True)
    require(operation is not None and job is not None, "RECOVERY_BINDING_NOT_FOUND")
    require(instance.status == "failed", "RECOVERY_INSTANCE_NOT_FAILED")
    require(execution.status in {"planned", "failed"}, "RECOVERY_EXECUTION_ACTIVE")
    valid_apply = apply.status == "uncertain" and apply.started_at is not None
    if finalize:
        valid_apply = valid_apply or (apply.status == "failed"
                                      and apply.error_code == "PROVISIONING_RESOURCE_CLEANED")
    require(valid_apply, "RECOVERY_APPLY_NOT_TAINTED_FAILURE")
    require(operation.kind == "instance.create" and operation.status == "failed", "RECOVERY_OPERATION_NOT_CREATE_FAILURE")
    require(job.status not in {"queued", "running"}, "RECOVERY_WRITER_ACTIVE")
    require(instance.vmid is None, "RECOVERY_INSTANCE_ALREADY_ADOPTED")
    require(execution.vmid is not None, "RECOVERY_ALLOCATION_REQUIRED")
    return instance, execution, apply, operation, job


def state_metadata(executor, context):
    workspace = executor.workspace(context["instance_id"], context["attempt"])
    env = executor.environment(workspace)
    reader = S3StateReader(context["backend"], env)
    try:
        snapshot = reader.read(cancelled)
        resource = state_resource(snapshot.state, context["variables"], allow_tainted=True)
        require(resource is not None, "RECOVERY_STATE_RESOURCE_MISSING")
        raw = snapshot.state["resources"]
        managed = [item for item in raw if item.get("mode") == "managed"]
        require(len(managed) == 1 and len(managed[0].get("instances", [])) == 1,
                "RECOVERY_STATE_NOT_UNIQUE")
        require(managed[0]["instances"][0].get("status") == "tainted", "RECOVERY_STATE_NOT_TAINTED")
        return snapshot, resource, env
    finally:
        reader.close()


def clear_protection(executor, context, env, evidence):
    """Retira solo la protección de la VM fallida ya acreditada.

    No elimina la VM ni toca otros recursos. Se exige identidad exacta,
    estado detenido y VM.Config.Options/VM.Allocate antes de la mutación;
    luego se vuelve a leer la configuración para sellar el resultado.
    """
    expected = context["variables"]["instance"]
    target = Target(expected["vm_id"], expected["node_name"], expected["vm_name"])
    tls = ssl.create_default_context(cafile=env.get("SSL_CERT_FILE"))
    client = ProxmoxPower(env["PROXMOX_VE_ENDPOINT"].rstrip("/"), env["PROXMOX_VE_API_TOKEN"],
                          build_opener(ProxyHandler({}), HTTPSHandler(context=tls), NoRedirect()))
    permissions = client._request("GET", "/access/permissions?path=/vms/" + str(target.vmid))
    privileges = permissions.get("/vms/" + str(target.vmid), {}) if isinstance(permissions, dict) else {}
    require(granted(privileges, "VM.Audit") and granted(privileges, "VM.Allocate"),
            "RECOVERY_PROTECTION_PERMISSION_DENIED")
    config = client._request("GET", target.path + "/config?current=1")
    status = client._request("GET", target.path + "/status/current")
    require(isinstance(config, dict) and config.get("name") == target.name
            and not config.get("template"), "RECOVERY_PROTECTION_IDENTITY_MISMATCH")
    require(isinstance(status, dict) and status.get("vmid") == target.vmid
            and status.get("status") == "stopped", "RECOVERY_VM_NOT_STOPPED")
    protected = config.get("protection") in (True, 1, "1")
    if protected:
        try:
            client._request("PUT", target.path + "/config", {"protection": 0})
        except PowerError as failure:
            raise PlanError("RECOVERY_PROTECTION_UPDATE_FAILED") from failure
        verified = client._request("GET", target.path + "/config?current=1")
        require(verified.get("name") == target.name and verified.get("protection", 0) in (False, 0, "0"),
                "RECOVERY_PROTECTION_UPDATE_UNCONFIRMED")
    write_json(evidence / "protection.json", {
        "vmid": target.vmid, "node": target.node, "name": target.name,
        "was_protected": protected, "verified_unprotected": True,
    })


def audit_event(session, operation, code, actor):
    event(session, operation, code, actor)
    session.commit()


def finalize_absent(session, instance_id, actor, evidence, executor, context):
    """Cierra el fence después de un destroy ya confirmado, sin tocar state."""
    instance = session.get(Instance, instance_id, with_for_update=True, populate_existing=True)
    execution = session.get(TerraformExecution, instance_id, with_for_update=True, populate_existing=True)
    apply = session.scalar(select(TerraformApply).where(
        TerraformApply.instance_id == instance_id, TerraformApply.attempt == execution.attempt
    ).with_for_update())
    operation = session.get(Operation, execution.operation_id, with_for_update=True)
    job = session.get(Job, execution.job_id, with_for_update=True)
    require(instance is not None and execution is not None and apply is not None
            and operation is not None and job is not None, "RECOVERY_FINALIZE_BINDING_NOT_FOUND")
    require(instance.status == "failed" and operation.status == "failed", "RECOVERY_FINALIZE_NOT_FAILED")
    require(job.status not in {"queued", "running"}, "RECOVERY_WRITER_ACTIVE")
    require(apply.error_code == "PROVISIONING_RESOURCE_CLEANED", "RECOVERY_FINALIZE_NOT_CLEANED")
    env = executor.environment(executor.workspace(instance_id, execution.attempt))
    reader = S3StateReader(context["backend"], env)
    try:
        snapshot = reader.read(cancelled)
        resource = state_resource(snapshot.state, context["variables"], allow_tainted=True)
        physical = proxmox_resource(env, context["variables"]["instance"], cancelled)
        require(resource is None and physical.get("present") is False, "RECOVERY_FINALIZE_ABSENCE_UNCONFIRMED")
        write_json(evidence / "finalize-preflight.json", {
            "at": stamp(), "instance_id": instance_id, "vmid": execution.vmid,
            "state": snapshot.metadata(), "physical": physical,
        })
    finally:
        reader.close()
    attempt = session.scalar(select(TerraformAttempt).where(
        TerraformAttempt.instance_id == instance_id, TerraformAttempt.number == execution.attempt
    ).with_for_update())
    execution.status = "failed"
    execution.error_code = "PROVISIONING_RESOURCE_CLEANED"
    apply.status = "failed"
    apply.error_code = "PROVISIONING_RESOURCE_CLEANED"
    if attempt is not None:
        attempt.status = "failed"
        attempt.error_code = "PROVISIONING_RESOURCE_CLEANED"
        attempt.finished_at = attempt.finished_at or datetime.now(timezone.utc)
    event(session, operation, "provisioning_failed_recovery_finalized", actor)
    session.commit()
    write_json(evidence / "finalize-db.json", {
        "at": stamp(), "instance_id": instance_id, "operation_id": operation.id,
        "execution_status": execution.status, "apply_status": apply.status,
        "attempt_status": attempt.status if attempt is not None else None,
        "instance_status": instance.status, "vmid": instance.vmid,
    })


def run(args):
    try:
        instance_id = str(UUID(args.instance_id))
    except ValueError:
        raise SystemExit("instance_id inválido")
    evidence = Path(args.evidence_dir).resolve()
    if evidence.exists():
        raise SystemExit("evidence-dir debe ser nuevo")
    evidence.mkdir(mode=0o700, parents=True)

    actor = args.actor
    factory = get_session_factory()
    with factory() as session:
        try:
            with instance_lock(session.get_bind(), instance_id):
                instance, execution, apply, operation, job = verify_database(
                    session, instance_id, finalize=args.finalize_only
                )
                allocation, context = context_for(instance, execution)
                executor = build_executor(observation=True)
                if args.finalize_only:
                    finalize_absent(session, instance_id, actor, evidence, executor, context)
                    print(json.dumps({"passed": True, "finalize_only": True, "evidence_dir": str(evidence)}, sort_keys=True))
                    return
                snapshot, resource, env = state_metadata(executor, context)
                physical = proxmox_resource(env, context["variables"]["instance"], cancelled)
                require(physical.get("present") is True, "RECOVERY_PROXMOX_ABSENT")
                require(physical.get("power_state") == "stopped", "RECOVERY_VM_NOT_STOPPED")
                write_json(evidence / "preflight.json", {
                    "at": stamp(), "instance_id": instance_id, "operation_id": operation.id,
                    "vmid": execution.vmid, "node": allocation.node_name, "name": instance.name,
                    "apply_id": apply.id, "apply_status": apply.status, "state": snapshot.metadata(),
                    "state_resource_id": resource.get("id"), "physical": physical,
                    "source_revision": SOURCE_REVISION,
                })
                if args.plan_only:
                    recovery = DeletionExecutor(executor.source, executor.artifacts, executor.runner)
                    result = recovery.plan_deletion(context, cancelled, allow_tainted=True)
                    write_json(evidence / "destroy-plan.json", result)
                    print(json.dumps({"passed": True, "plan_only": True, "evidence_dir": str(evidence)}, sort_keys=True))
                    return

                audit_event(session, operation, "provisioning_failed_resource_destroy_started", actor)
                recovery = DeletionExecutor(executor.source, executor.artifacts, executor.runner)
                planned = recovery.plan_deletion(context, cancelled, allow_tainted=True)
                write_json(evidence / "destroy-plan.json", planned)
                clear_protection(executor, context, env, evidence)

                def before_mutation():
                    with factory() as locked:
                        current = locked.get(Operation, operation.id, with_for_update=True)
                        audit_event(locked, current, "provisioning_failed_resource_destroy_consumed", actor)

                try:
                    result = recovery.apply_deletion(
                        context, planned["plan_sha256"], cancelled, before_mutation, allow_tainted=True
                    )
                except PlanError as failure:
                    # Solo metadatos de fase/comando/código; nunca stdout, stderr
                    # ni argumentos que puedan contener credenciales.
                    write_json(evidence / "failure.json", {
                        "at": stamp(), "code": failure.code,
                        "diagnostic": failure.diagnostic(),
                    })
                    raise
                write_json(evidence / "destroy-result.json", result)

                # El destroy ya confirmó ausencia física y ausencia del recurso
                # en state. El CREATE sigue fallido; solo se cierra el fence de
                # recuperación y se conserva toda su trazabilidad.
                current = session.get(TerraformApply, apply.id, with_for_update=True)
                current.status = "failed"
                current.error_code = "PROVISIONING_RESOURCE_CLEANED"
                current.result = {**(current.result or {}), "recovery": {
                    "mode": "tainted_destroy", "actor": actor, "vmid": execution.vmid,
                    "evidence_dir": str(evidence), "destroyed": True,
                }}
                current.finished_at = current.finished_at or datetime.now(timezone.utc)
                execution.error_code = "PROVISIONING_RESOURCE_CLEANED"
                operation.error_code = "PROVISIONING_RESOURCE_CLEANED"
                instance.status = "failed"
                instance.vmid = None
                instance.node = None
                audit_event(session, operation, "provisioning_failed_resource_destroy_completed", actor)
                write_json(evidence / "final-db.json", {
                    "at": stamp(), "instance_id": instance_id, "operation_id": operation.id,
                    "apply_status": current.status, "error_code": current.error_code,
                    "instance_status": instance.status, "vmid": instance.vmid,
                })
        except Exception:
            session.rollback()
            raise
    print(json.dumps({"passed": True, "plan_only": False, "evidence_dir": str(evidence)}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--actor", default="lab-operator:tainted-recovery")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--finalize-only", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except (PlanError, ExecutionBusy) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
