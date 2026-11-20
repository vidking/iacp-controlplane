#!/usr/bin/env python3
"""Borrar solo los datos locales de una matriz IAC ya destruida y evidenciada."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from api.db import database
from worker.proxmox import ProxmoxClient


RUN_ID_PATTERN = re.compile(r"^\d{14}$")
IMAGES = ("ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10")
PLANS = ("kvm-1", "kvm-2", "kvm-3", "kvm-4")
ACTIVE_STATUSES = ("pending", "running", "planning", "planned", "applying", "destroying", "powering")
RUN_PREFIX = "e2e-iac-prod-mng-"


class PurgeRefused(RuntimeError):
    pass


def _sql_placeholders(count: int) -> str:
    if count < 1:
        raise PurgeRefused("una consulta de purga requiere al menos un ID")
    return ",".join("?" for _ in range(count))


def _instance_names(run_id: str) -> set[str]:
    prefix = f"{RUN_PREFIX}{run_id}-"
    return {
        f"{prefix}{index:02d}-{image.replace('-', '')}-{plan}"
        for index, (image, plan) in enumerate(
            ((image, plan) for image in IMAGES for plan in PLANS),
            start=1,
        )
    }


def _load_evidence(run_id: str, evidence_path: Path, workdata_root: Path) -> tuple[dict, str]:
    resolved_evidence = evidence_path.expanduser().resolve(strict=True)
    try:
        resolved_evidence.relative_to(workdata_root.resolve(strict=True))
    except ValueError:
        pass
    else:
        raise PurgeRefused("la evidencia debe quedar fuera del volumen de datos que se purgará")
    raw = resolved_evidence.read_bytes()
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PurgeRefused("la evidencia JSON no se pudo interpretar") from error
    if not isinstance(report, dict):
        raise PurgeRefused("la evidencia no tiene el formato de reporte esperado")
    if (
        report.get("run_id") != run_id
        or report.get("base_url") != "https://iac.cyberfuel.com"
        or report.get("runtime_stage") != "production"
        or report.get("environment") != "lab"
        or report.get("cluster") != "MNG"
        or report.get("vmid_range") != [30000, 39999]
        or report.get("max_simultaneous_provisions") != 6
        or report.get("images") != list(IMAGES)
        or report.get("plans") != list(PLANS)
        or report.get("outcome") != "PASS"
    ):
        raise PurgeRefused("la evidencia no certifica PASS para esta corrida de iac.cyberfuel.com en MNG")
    metrics = report.get("metrics") or {}
    if (
        metrics.get("contention_detected")
        or metrics.get("sampling_errors")
        or metrics.get("foreign_activity")
        or int(metrics.get("peak_test_busy_workers", 0)) < 6
    ):
        raise PurgeRefused("la evidencia no confirma aislamiento ni simultaneidad de los seis workers de prueba")
    results = report.get("results")
    expected = _instance_names(run_id)
    if not isinstance(results, list) or len(results) != 16:
        raise PurgeRefused("la evidencia no contiene exactamente los 16 casos de la matriz")
    if {item.get("name") for item in results if isinstance(item, dict)} != expected:
        raise PurgeRefused("los nombres de la evidencia no coinciden con las 16 combinaciones esperadas")
    for item in results:
        if (
            not isinstance(item, dict)
            or item.get("status") != "passed"
            or not item.get("instance_id")
            or not item.get("create_operation_id")
            or not item.get("operation_ids")
            or not 30000 <= int(item.get("vmid", -1)) <= 39999
        ):
            raise PurgeRefused("la evidencia contiene un caso incompleto o fuera del rango autorizado")
    digest = hashlib.sha256(raw).hexdigest()
    return report, digest


def _state_resources(value: dict) -> list[tuple[object, object]]:
    found: list[tuple[object, object]] = []

    def visit_module(module: object) -> None:
        if not isinstance(module, dict):
            return
        for resource in module.get("resources") or []:
            if not isinstance(resource, dict):
                raise PurgeRefused("Terraform State contiene una estructura de recurso inválida")
            for instance in resource.get("instances") or []:
                if not isinstance(instance, dict):
                    raise PurgeRefused("Terraform State contiene una instancia de recurso inválida")
                attributes = instance.get("attributes") or {}
                if not isinstance(attributes, dict):
                    raise PurgeRefused("Terraform State contiene atributos de recurso inválidos")
                found.append((attributes.get("name"), attributes.get("vm_id", attributes.get("vmid"))))
        for child in module.get("child_modules") or []:
            visit_module(child)

    for resource in value.get("resources") or []:
        if not isinstance(resource, dict):
            raise PurgeRefused("Terraform State contiene una estructura de recurso inválida")
        for instance in resource.get("instances") or []:
            if not isinstance(instance, dict):
                raise PurgeRefused("Terraform State contiene una instancia de recurso inválida")
            attributes = instance.get("attributes") or {}
            if not isinstance(attributes, dict):
                raise PurgeRefused("Terraform State contiene atributos de recurso inválidos")
            found.append((attributes.get("name"), attributes.get("vm_id", attributes.get("vmid"))))
    values = value.get("values") or {}
    if isinstance(values, dict):
        visit_module(values.get("root_module"))
    return found


def _verify_state(workspace_root: Path, name: str, vmid: int) -> dict:
    state_directory = workspace_root / "environments" / "mng01"
    active_state = state_directory / "terraform.tfstate"
    if (
        state_directory.is_symlink()
        or active_state.is_symlink()
        or not state_directory.is_dir()
        or not active_state.is_file()
    ):
        raise PurgeRefused("falta el Terraform State final esperado de una instancia de la matriz")
    lock_paths = [
        path
        for path in workspace_root.rglob("*")
        if path.name.endswith(".tflock") or path.name in {".terraform.tfstate.lock.info", "terraform.tfstate.lock.info"}
    ]
    if lock_paths:
        raise PurgeRefused("hay un lock de Terraform en un workspace de la matriz")

    state_files = [path for path in state_directory.glob("terraform.tfstate*") if path.is_file()]
    state_refs = 0
    for state_path in state_files:
        if state_path.is_symlink():
            raise PurgeRefused("un Terraform State de la corrida es un symlink")
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PurgeRefused("un archivo Terraform State no se pudo validar") from error
        if not isinstance(state_data, dict):
            raise PurgeRefused("un archivo Terraform State tiene un formato inválido")
        references = _state_resources(state_data)
        if state_path == active_state and references:
            raise PurgeRefused("el Terraform State activo todavía contiene recursos")
        for resource_name, resource_vmid in references:
            try:
                normalized_vmid = int(resource_vmid)
            except (TypeError, ValueError) as error:
                raise PurgeRefused("un backup de Terraform State tiene identidad de VM incompleta") from error
            if resource_name != name or normalized_vmid != vmid:
                raise PurgeRefused("un backup de Terraform State referencia recursos fuera de esta instancia")
            state_refs += 1
    return {"state_files": len(state_files), "backup_resource_references": state_refs}


def _assert_idle(db) -> None:
    placeholders = _sql_placeholders(len(ACTIVE_STATUSES))
    active = db.execute(
        f"SELECT COUNT(*) AS count FROM operations WHERE status IN ({placeholders})",
        ACTIVE_STATUSES,
    ).fetchone()["count"]
    queued = db.execute("SELECT COUNT(*) AS count FROM operations WHERE status='pending'").fetchone()["count"]
    uncertain = db.execute("SELECT COUNT(*) AS count FROM operations WHERE status='uncertain'").fetchone()["count"]
    workers = db.execute("SELECT worker_id, status, current_operation_id FROM workers ORDER BY worker_id").fetchall()
    if active or queued or uncertain:
        raise PurgeRefused("la cola compartida contiene operaciones activas o uncertain")
    if len(workers) != 6 or any(row["status"] != "idle" or row["current_operation_id"] for row in workers):
        raise PurgeRefused("se requieren seis workers idle y sin operación asignada para purgar")


def _verify_vmid_leases(instances: list[dict], provision_by_instance: dict, lease_rows: list[dict]) -> dict:
    latest_by_vmid = {}
    for instance in instances:
        provision = provision_by_instance.get(instance["instance_id"])
        if provision is None:
            raise PurgeRefused("falta la operación de aprovisionamiento para una lease VMID")
        vmid = int(instance["vmid"])
        timestamp = provision.get("started_at") or provision["created_at"]
        current = latest_by_vmid.get(vmid)
        if current is None or timestamp > current[0]:
            latest_by_vmid[vmid] = (timestamp, provision["operation_id"])

    leases_by_vmid = {int(row["vmid"]): row for row in lease_rows}
    if set(leases_by_vmid) != set(latest_by_vmid):
        raise PurgeRefused("las leases actuales no cubren exactamente los VMID únicos de la corrida")
    for vmid, (_, operation_id) in latest_by_vmid.items():
        lease = leases_by_vmid[vmid]
        if lease["status"] != "released" or lease["operation_id"] != operation_id:
            raise PurgeRefused("un lease de VMID vigente no está liberado o no corresponde al último aprovisionamiento")
    return {
        "unique_vmids": len(latest_by_vmid),
        "reused_instance_allocations": len(instances) - len(latest_by_vmid),
        "current_leases_released": True,
    }


def _read_plan(db, run_id: str, workdata_root: Path) -> dict:
    _assert_idle(db)
    prefix = f"{RUN_PREFIX}{run_id}-"
    instances = db.execute(
        "SELECT * FROM instances WHERE name LIKE ? ORDER BY name",
        (f"{prefix}%",),
    ).fetchall()
    expected_names = _instance_names(run_id)
    if {row["name"] for row in instances} != expected_names or len(instances) != 16:
        raise PurgeRefused("PostgreSQL no contiene exactamente las 16 instancias esperadas")
    image_plan_pairs = set()
    for row in instances:
        if row["status"] != "deleted" or row["environment"] != "lab":
            raise PurgeRefused("una instancia de la corrida no está eliminada o no pertenece a lab")
        vmid = row["vmid"]
        if vmid is None or not 30000 <= int(vmid) <= 39999:
            raise PurgeRefused("una instancia de la corrida no tiene VMID válido de iac.cyberfuel.com")
        image_plan_pairs.add((row["image"], row["plan_id"]))
    if image_plan_pairs != {(image, plan) for image in IMAGES for plan in PLANS}:
        raise PurgeRefused("la matriz persistida no contiene cada combinación única de imagen y plan")

    instance_ids = [row["instance_id"] for row in instances]
    instance_placeholders = _sql_placeholders(len(instance_ids))
    operations = db.execute(
        f"SELECT * FROM operations WHERE instance_id IN ({instance_placeholders}) ORDER BY created_at, operation_id",
        instance_ids,
    ).fetchall()
    if not operations or any(row["status"] != "succeeded" for row in operations):
        raise PurgeRefused("todas las operaciones de la corrida deben estar en estado terminal exitoso")
    operation_ids = {row["operation_id"] for row in operations}
    provision_by_instance = {
        row["instance_id"]: row
        for row in operations
        if row["command"] == "provision" and row["action"] == "provision"
    }
    if len(provision_by_instance) != 16:
        raise PurgeRefused("no se encontraron las 16 operaciones originales de aprovisionamiento")

    used_vmids = sorted({int(row["vmid"]) for row in instances})
    vmid_placeholders = _sql_placeholders(len(used_vmids))
    lease_rows = db.execute(
        f"SELECT * FROM vmid_leases WHERE vmid IN ({vmid_placeholders})",
        used_vmids,
    ).fetchall()
    lease_checks = _verify_vmid_leases(instances, provision_by_instance, lease_rows)

    allocations = db.execute(
        f"SELECT COUNT(*) AS count FROM ip_allocations WHERE instance_id IN ({instance_placeholders})",
        instance_ids,
    ).fetchone()["count"]
    if allocations:
        raise PurgeRefused("hay asignaciones IPAM; deben liberarse por el flujo administrado antes de purgar")

    idempotency_rows = db.execute(
        "SELECT operation_id FROM idempotency_keys WHERE idempotency_key LIKE ?",
        (f"{run_id}-%",),
    ).fetchall()
    if any(row["operation_id"] not in operation_ids for row in idempotency_rows):
        raise PurgeRefused("se detectaron llaves de idempotencia de la corrida sin operación asociada")

    plans_root = (workdata_root / "plans").resolve(strict=True)
    secret_root_path = workdata_root / "operation-secrets"
    if (workdata_root / "plans").is_symlink() or secret_root_path.is_symlink():
        raise PurgeRefused("las carpetas de Terraform o secretos no pueden ser symlinks")
    secret_root = secret_root_path.resolve(strict=False)
    try:
        plans_root.relative_to(workdata_root)
        secret_root.relative_to(workdata_root)
    except ValueError as error:
        raise PurgeRefused("las carpetas de Terraform o secretos salen de IACP_WORKDATA_DIR") from error
    workspaces: dict[str, Path] = {}
    secrets_to_remove: set[Path] = set()
    state_checks = {}
    for instance in instances:
        provision = provision_by_instance[instance["instance_id"]]
        operation_id = str(provision["operation_id"])
        try:
            uuid.UUID(operation_id)
        except ValueError as error:
            raise PurgeRefused("una operación tiene un identificador inválido para resolver sus archivos") from error
        expected_operation_dir = plans_root / operation_id
        expected_workspace = expected_operation_dir / "terraform"
        if expected_operation_dir.is_symlink() or expected_workspace.is_symlink():
            raise PurgeRefused("un directorio aislado de la corrida es un symlink")
        resolved_operation_dir = expected_operation_dir.resolve(strict=True)
        resolved_workspace = expected_workspace.resolve(strict=True)
        if resolved_operation_dir.parent != plans_root or resolved_workspace != expected_workspace:
            raise PurgeRefused("un workspace de la corrida no está dentro de su carpeta aislada")
        if provision["workspace_path"] and Path(provision["workspace_path"]).resolve(strict=True) != resolved_workspace:
            raise PurgeRefused("el workspace registrado no coincide con el directorio aislado de la operación")
        if provision["plan_path"] and Path(provision["plan_path"]).resolve(strict=False) != expected_operation_dir / "plan.tfplan":
            raise PurgeRefused("el plan Terraform registrado sale de la carpeta aislada de su operación")
        workspaces[operation_id] = resolved_operation_dir
        state_checks[instance["name"]] = _verify_state(
            resolved_workspace,
            instance["name"],
            int(instance["vmid"]),
        )
        for operation in operations:
            if operation["instance_id"] != instance["instance_id"]:
                continue
            if operation["workspace_path"] and Path(operation["workspace_path"]).resolve(strict=True) != resolved_workspace:
                raise PurgeRefused("una operación registrada apunta fuera del workspace de su instancia")
            if operation["plan_path"] and Path(operation["plan_path"]).resolve(strict=False) != expected_operation_dir / "plan.tfplan":
                raise PurgeRefused("una operación apunta a un plan Terraform fuera de su carpeta aislada")
        secret = provision["secret_path"]
        if secret:
            expected_secret = secret_root / f"{operation_id}.root_password"
            if Path(secret).resolve(strict=False) != expected_secret:
                raise PurgeRefused("una ruta de secreto de la corrida no coincide con su nombre aislado")
            secrets_to_remove.add(expected_secret)

    return {
        "instances": instances,
        "operations": operations,
        "instance_ids": instance_ids,
        "operation_ids": sorted(operation_ids),
        "vmids": sorted(int(row["vmid"]) for row in instances),
        "workspaces": workspaces,
        "secrets": secrets_to_remove,
        "state_checks": state_checks,
        "idempotency_rows": len(idempotency_rows),
        "lease_checks": lease_checks,
    }


def _verify_physical_absence(plan: dict) -> None:
    endpoint = os.getenv("IACP_PROXMOX_MNG_ENDPOINT", "").strip()
    token = os.getenv("IACP_PROXMOX_MNG_API_TOKEN", "").strip()
    credential_file = os.getenv(
        "IACP_PROXMOX_MNG_CREDENTIALS_FILE",
        os.getenv("IACP_PROXMOX_CREDENTIALS_FILE", "/run/secrets/proxmox_credentials"),
    ).strip()
    if not endpoint or not token:
        try:
            credentials = json.loads(Path(credential_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PurgeRefused("faltan credenciales de solo lectura del clúster MNG") from error
        endpoint = endpoint or str(credentials.get("PROXMOX_VE_ENDPOINT") or credentials.get("endpoint") or "").strip()
        token = token or str(credentials.get("PROXMOX_VE_API_TOKEN") or credentials.get("api_token") or "").strip()
    if not endpoint.startswith("https://") or not token:
        raise PurgeRefused("credenciales de MNG incompletas o endpoint no HTTPS")
    inventory = ProxmoxClient(endpoint, token).inventory()
    if not isinstance(inventory, list):
        raise PurgeRefused("el inventario del clúster MNG no se pudo verificar")
    present = {
        int(item["vmid"])
        for item in inventory
        if isinstance(item, dict) and str(item.get("vmid", "")).isdigit()
    }
    survivors = sorted(set(plan["vmids"]) & present)
    if survivors:
        raise PurgeRefused(f"siguen presentes VMID de la matriz en MNG: {survivors}")


def _same_snapshot(first: dict, second: dict) -> bool:
    return (
        first["instance_ids"] == second["instance_ids"]
        and first["operation_ids"] == second["operation_ids"]
        and first["vmids"] == second["vmids"]
    )


def _delete_files(plan: dict) -> None:
    for directory in plan["workspaces"].values():
        if directory.exists():
            if directory.is_symlink():
                raise PurgeRefused("no se elimina una carpeta de workspace que sea symlink")
            shutil.rmtree(directory)
    for secret in plan["secrets"]:
        if secret.exists():
            secret.unlink()


def _delete_database_rows(db, plan: dict, run_id: str) -> None:
    instance_ids = plan["instance_ids"]
    operation_ids = plan["operation_ids"]
    instance_placeholders = _sql_placeholders(len(instance_ids))
    operation_placeholders = _sql_placeholders(len(operation_ids))
    db.execute(
        f"DELETE FROM events WHERE operation_id IN ({operation_placeholders})",
        operation_ids,
    )
    db.execute(
        f"DELETE FROM idempotency_keys WHERE operation_id IN ({operation_placeholders}) OR idempotency_key LIKE ?",
        (*operation_ids, f"{run_id}-%"),
    )
    db.execute(
        f"DELETE FROM ip_allocations WHERE instance_id IN ({instance_placeholders})",
        instance_ids,
    )
    db.execute(
        f"DELETE FROM vmid_leases WHERE operation_id IN ({operation_placeholders})",
        operation_ids,
    )
    db.execute(
        f"DELETE FROM operations WHERE operation_id IN ({operation_placeholders}) OR instance_id IN ({instance_placeholders})",
        (*operation_ids, *instance_ids),
    )
    db.execute(
        f"DELETE FROM instances WHERE instance_id IN ({instance_placeholders})",
        instance_ids,
    )
    remaining = db.execute(
        f"SELECT COUNT(*) AS count FROM instances WHERE instance_id IN ({instance_placeholders})",
        instance_ids,
    ).fetchone()["count"]
    remaining_operations = db.execute(
        f"SELECT COUNT(*) AS count FROM operations WHERE operation_id IN ({operation_placeholders})",
        operation_ids,
    ).fetchone()["count"]
    if remaining or remaining_operations:
        raise PurgeRefused("la verificación PostgreSQL detectó filas residuales tras la purga")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="ID UTC de 14 dígitos de una matriz PASS")
    parser.add_argument("--evidence-file", required=True, type=Path, help="evidencia JSON conservada fuera de IACP_WORKDATA_DIR")
    parser.add_argument("--execute", action="store_true", help="ejecuta la purga exacta después de validar todas las precondiciones")
    args = parser.parse_args()
    if not RUN_ID_PATTERN.fullmatch(args.run_id):
        raise PurgeRefused("--run-id debe contener exactamente 14 dígitos UTC")

    workdata_root = Path(os.getenv("IACP_WORKDATA_DIR", "/var/lib/iacp-controlplane")).expanduser().resolve(strict=True)
    plans_root = workdata_root / "plans"
    if not plans_root.is_dir():
        raise PurgeRefused("no existe IACP_WORKDATA_DIR/plans")
    report, evidence_sha256 = _load_evidence(args.run_id, args.evidence_file, workdata_root)

    with database() as db:
        plan = _read_plan(db, args.run_id, workdata_root)
    evidence_instances = {item["name"]: item for item in report["results"]}
    operations_by_instance: dict[str, set[str]] = {}
    for operation in plan["operations"]:
        operations_by_instance.setdefault(operation["instance_id"], set()).add(operation["operation_id"])
    for row in plan["instances"]:
        item = evidence_instances[row["name"]]
        if (
            item["instance_id"] != row["instance_id"]
            or item["create_operation_id"] != row["operation_id"]
            or set(item["operation_ids"]) != operations_by_instance.get(row["instance_id"], set())
            or int(item["vmid"]) != int(row["vmid"])
        ):
            raise PurgeRefused("la evidencia y PostgreSQL no identifican las mismas instancias y VMID")
    _verify_physical_absence(plan)

    safe_summary = {
        "run_id": args.run_id,
        "evidence_sha256": evidence_sha256,
        "instances": len(plan["instances"]),
        "operations": len(plan["operations"]),
        "idempotency_keys": plan["idempotency_rows"],
        "vmids": plan["vmids"],
        "workspaces": len(plan["workspaces"]),
        "secret_files_present": sum(secret.is_file() for secret in plan["secrets"]),
        "state_checks": plan["state_checks"],
        "lease_checks": plan["lease_checks"],
        "mode": "execute" if args.execute else "dry-run",
    }
    if not args.execute:
        print(json.dumps({"status": "READY", **safe_summary}, sort_keys=True))
        return 0

    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            fresh_plan = _read_plan(db, args.run_id, workdata_root)
            if not _same_snapshot(plan, fresh_plan):
                raise PurgeRefused("la corrida cambió entre la validación y la purga")
            _verify_physical_absence(fresh_plan)
            for row in fresh_plan["instances"]:
                original = evidence_instances[row["name"]]
                if (
                    original["instance_id"] != row["instance_id"]
                    or original["create_operation_id"] != row["operation_id"]
                    or set(original["operation_ids"]) != operations_by_instance.get(row["instance_id"], set())
                    or int(original["vmid"]) != int(row["vmid"])
                ):
                    raise PurgeRefused("la evidencia dejó de coincidir con PostgreSQL")
            _delete_files(fresh_plan)
            _delete_database_rows(db, fresh_plan, args.run_id)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    print(json.dumps({"status": "PURGED", **safe_summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PURGE REFUSED: {error}", file=sys.stderr)
        raise SystemExit(2)
