"""Purga administrativa, acotada y explícita de historial KVM.

La purga total es una operación de mantenimiento, no una ruta HTTP. El rango
se resuelve primero a UUIDs e identidades técnicas exactas; nunca se elimina
un prefijo completo de State ni se interpreta la ausencia física como permiso
para borrar un recurso cuyo ownership no coincide.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Callable, Iterable

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import delete, or_, select, text

from ..db import get_session_factory
from ..models import (
    CommercialIntent,
    DeletionExecution,
    GuestInitialization,
    Instance,
    Job,
    NetworkDiscovery,
    Operation,
    OperationEvent,
    PowerExecution,
    PowerOwnershipCertificate,
    TechnicalOwnership,
    TerraformApply,
    TerraformAttempt,
    TerraformExecution,
)
from ..services.commercial_bootstrap_recovery import assert_broker_and_workers_idle
from ..terraform.config import Allocation, Backend, PlanError
from ..terraform.lifecycle import build_executor
from ..terraform.locks import admission_lock
from ..terraform.profiles import cluster_vmids
from ..terraform.state import S3StateReader


MIN_VMID = 100
MAX_VMID = 999_999_999
ACTIVE_OPERATION_STATUSES = frozenset({"queued", "running"})
ACTIVE_APPLY_STATUSES = frozenset({"approved", "queued", "running"})
ACTIVE_EXECUTION_STATUSES = frozenset({"queued", "running"})
ACTIVE_POWER_STATUSES = frozenset({"queued", "running"})
ACTIVE_DELETE_STATUSES = frozenset({"queued", "running"})
ACTIVE_GUEST_STATUSES = frozenset({"queued", "running"})

# La VM 106 ya no se trata como protegida. Su inclusión requiere un rango
# explícito del operador y pasa las mismas comprobaciones de ausencia técnica.
PROTECTED_VMIDS = frozenset({104, 29000, 29001, 29010, 29011})
FENCE_EVENT_CODES = frozenset({
    "commercial_bootstrap_dispatch_consumed",
    "commercial_bootstrap_uncertain",
})
FENCE_FORBIDDEN_EVENT_CODES = frozenset({
    "commercial_bootstrap_completed",
    "commercial_bootstrap_credentials_delivered",
    "commercial_bootstrap_ssh_connected",
})
FENCE_CONFIRMATION_VERSION = "iacp.readonly-operation-fence.v1"

DELETE_ORDER = (
    "power_executions",
    "deletion_executions",
    "guest_initializations",
    "network_discoveries",
    "operation_events",
    "power_ownership_certificates",
    "terraform_applies",
    "terraform_attempts",
    "terraform_executions",
    "commercial_intents",
    "technical_ownership",
    "jobs",
    "operations",
    "instances",
)


class PurgeError(RuntimeError):
    """Fallo cerrado de una purga administrativa."""


@dataclass(frozen=True)
class PurgeTarget:
    instance_id: str
    tenant_id: str
    vmid: int
    node: str | None
    name: str
    state_key: str
    allocation: dict
    binding_source: str = "technical-ownership"

    def public(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "tenant_id": self.tenant_id,
            "vmid": self.vmid,
            "node": self.node,
            "name": self.name,
            "state_key_sha256": hashlib.sha256(self.state_key.encode("utf8")).hexdigest(),
            "binding_source": self.binding_source,
        }


def parse_vmid_range(value: str) -> tuple[int, int]:
    """Parsea ``inicio-fin`` y rechaza rangos ambiguos o fuera de límites."""
    if not isinstance(value, str):
        raise PurgeError("VMID_RANGE_INVALID")
    match = re.fullmatch(r"\s*(\d+)\s*[-–—:]\s*(\d+)\s*", value)
    if not match:
        raise PurgeError("VMID_RANGE_INVALID")
    start, end = (int(item) for item in match.groups())
    if not MIN_VMID <= start <= MAX_VMID or not MIN_VMID <= end <= MAX_VMID:
        raise PurgeError("VMID_RANGE_OUT_OF_BOUNDS")
    if start > end:
        raise PurgeError("VMID_RANGE_REVERSED")
    return start, end


def total_purge_confirmation(start: int, end: int) -> str:
    return f"PURGAR TOTAL {start}-{end}"


def validate_total_purge_confirmation(
    start: int, end: int, confirmation: str | None
) -> None:
    if confirmation != total_purge_confirmation(start, end):
        raise PurgeError("TOTAL_PURGE_CONFIRMATION_MISMATCH")


def _ids_in_range(session, start: int, end: int) -> set[str]:
    statements = (
        select(Instance.id).where(Instance.vmid.between(start, end)),
        select(TerraformExecution.instance_id).where(
            TerraformExecution.vmid.between(start, end)
        ),
        select(TechnicalOwnership.instance_id).where(
            TechnicalOwnership.vmid.between(start, end)
        ),
        select(PowerExecution.instance_id).where(PowerExecution.vmid.between(start, end)),
        select(DeletionExecution.instance_id).where(
            DeletionExecution.vmid.between(start, end)
        ),
    )
    result: set[str] = set()
    for statement in statements:
        result.update(session.scalars(statement).all())
    return result


def _validated_allocation_binding(instance, execution) -> dict:
    """Valida la asignación histórica que liga instancia, VMID, nodo y State.

    Algunas instancias eliminadas son anteriores a la migración que materializa
    ``TechnicalOwnership``. En ese caso la evidencia no se infiere: se acepta
    únicamente si el snapshot de ``TerraformExecution`` sigue describiendo
    exactamente la instancia y su State key autorizado.
    """
    if execution is None or not execution.state_key or not isinstance(execution.allocation, dict):
        raise PurgeError("PURGE_TECHNICAL_BINDING_MISSING")
    try:
        allocation = Allocation.model_validate(execution.allocation)
    except (TypeError, ValueError):
        raise PurgeError("PURGE_LEGACY_ALLOCATION_INVALID") from None

    if (
        execution.vmid is None
        or (instance.vmid is not None and instance.vmid != execution.vmid)
        or allocation.vm_id != execution.vmid
        or (instance.node is not None and allocation.node_name != instance.node)
        or allocation.image != instance.image
        or allocation.environment != "lab"
        or allocation.node_name not in allocation.allowed_nodes
        or f"/tenants/{instance.tenant_id}/instances/{instance.id}/" not in execution.state_key
        or (
            (instance.vmid is None or instance.node is None)
            and instance.status not in {"failed", "deleted"}
        )
    ):
        raise PurgeError("PURGE_LEGACY_BINDING_MISMATCH")
    return execution.allocation


def collect_targets(session, start: int, end: int) -> list[PurgeTarget]:
    """Resolve un rango a bindings exactos, sin aceptar State ambiguo."""
    identifiers = _ids_in_range(session, start, end)
    if not identifiers:
        return []

    targets: list[PurgeTarget] = []
    for instance in session.scalars(
        select(Instance).where(Instance.id.in_(identifiers)).order_by(Instance.id)
    ).all():
        execution = session.get(TerraformExecution, instance.id)
        ownership = session.get(TechnicalOwnership, instance.id)
        allocation = _validated_allocation_binding(instance, execution)
        binding_source = "legacy-execution-allocation"
        ownership_vmid = None
        if ownership is not None:
            if (
                ownership.tenant_id != instance.tenant_id
                or ownership.vmid != execution.vmid
                or ownership.name != instance.name
                or ownership.state_key != execution.state_key
                or ownership.node != (instance.node or allocation["node_name"])
            ):
                raise PurgeError("PURGE_TECHNICAL_BINDING_MISMATCH")
            ownership_vmid = ownership.vmid
            binding_source = "technical-ownership"

        vmids = {
            value for value in (instance.vmid, execution.vmid, ownership_vmid) if value is not None
        }
        if len(vmids) != 1:
            raise PurgeError("PURGE_VMID_BINDING_MISMATCH")
        vmid = vmids.pop()
        if not start <= vmid <= end:
            raise PurgeError("PURGE_RANGE_BINDING_MISMATCH")
        if vmid in PROTECTED_VMIDS:
            raise PurgeError("PURGE_PROTECTED_VMID")

        targets.append(
            PurgeTarget(
                instance_id=instance.id,
                tenant_id=instance.tenant_id,
                vmid=vmid,
                node=instance.node or (ownership.node if ownership is not None else allocation["node_name"]),
                name=instance.name,
                state_key=execution.state_key,
                allocation=allocation,
                binding_source=binding_source,
            )
        )
    if {target.instance_id for target in targets} != identifiers:
        raise PurgeError("PURGE_TARGET_RESOLUTION_INCOMPLETE")
    return targets


def _contains_sensitive_key(value) -> bool:
    if isinstance(value, dict):
        if any(
            re.search(r"password|secret|token|private.?key|ciphertext", str(key), re.I)
            for key in value
        ):
            return True
        return any(_contains_sensitive_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def validate_fence_evidence(
    evidence: dict, target: PurgeTarget, operation: Operation, job: Job,
) -> None:
    """Valida una atestación read-only exacta para una operación uncertain.

    La evidencia la genera el verificador canónico de go_live. Esta función
    solo comprueba el binding y no convierte la operación en terminal.
    """
    if not isinstance(evidence, dict) or _contains_sensitive_key(evidence):
        raise PurgeError("PURGE_FENCE_EVIDENCE_INVALID")
    expected = {
        "operation_id": operation.id,
        "instance_id": target.instance_id,
        "vmid": target.vmid,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise PurgeError("PURGE_FENCE_BINDING_MISMATCH")
    evidence_version = evidence.get("evidence_version")
    evidence_mode = evidence.get("mode")
    if evidence_version not in {
        FENCE_CONFIRMATION_VERSION,
        "iacp.bootstrap-deleted-absence.v1",
    } and evidence_mode not in {"readonly-operation-fence", "readonly-deleted-absence"}:
        raise PurgeError("PURGE_FENCE_EVIDENCE_VERSION_UNSUPPORTED")
    if evidence.get("operation_kind") != "instance.access.bootstrap":
        raise PurgeError("PURGE_FENCE_OPERATION_KIND_MISMATCH")
    if evidence.get("operation_status") != "uncertain" or operation.status != "uncertain":
        raise PurgeError("PURGE_FENCE_OPERATION_STATUS_MISMATCH")
    if evidence.get("job_status") != "failed" or job.status != "failed":
        raise PurgeError("PURGE_FENCE_JOB_STATUS_MISMATCH")
    if evidence.get("redelivery_mode") != "observe-only-fenced":
        raise PurgeError("PURGE_FENCE_REDELIVERY_UNSAFE")
    if (
        evidence.get("proxmox_present") is not False
        or evidence.get("state_resource_present") is not False
        or evidence.get("state_lock_absent") is not True
    ):
        raise PurgeError("PURGE_FENCE_ABSENCE_UNCONFIRMED")
    if evidence.get("state_resource_id") not in (None, ""):
        raise PurgeError("PURGE_FENCE_STATE_RESOURCE_PRESENT")
    if not isinstance(evidence.get("state_snapshot"), dict):
        raise PurgeError("PURGE_FENCE_STATE_SNAPSHOT_MISSING")


def _advisory_locks(session) -> int:
    if session.get_bind().dialect.name != "postgresql":
        return 0
    return int(
        session.scalar(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid <> pg_backend_pid()"
            )
        )
        or 0
    )


def validate_database_activity(
    session,
    targets: Iterable[PurgeTarget],
    fence_evidence: dict[str, dict] | None = None,
) -> dict:
    """Comprueba que el rango no tiene trabajo ejecutable."""
    identifiers = [target.instance_id for target in targets]
    if not identifiers:
        return {"active_operations": [], "active_jobs": [], "advisory_locks": 0}
    fence_evidence = fence_evidence or {}

    instances = session.scalars(
        select(Instance).where(Instance.id.in_(identifiers))
    ).all()
    if any(instance.status not in {"deleted", "failed"} for instance in instances):
        raise PurgeError("PURGE_INSTANCE_NOT_TERMINAL")

    operations = session.scalars(
        select(Operation).where(
            Operation.instance_id.in_(identifiers),
            Operation.status.in_(ACTIVE_OPERATION_STATUSES | {"uncertain"}),
        )
    ).all()
    operation_reports = []
    target_by_id = {target.instance_id: target for target in targets}
    for operation in operations:
        if operation.status != "uncertain":
            raise PurgeError("PURGE_NONTERMINAL_OPERATION")
        job = session.scalar(
            select(Job).where(Job.operation_id == operation.id)
        )
        target = target_by_id[operation.instance_id]
        evidence = fence_evidence.get(operation.id)
        if job is None or evidence is None:
            raise PurgeError("PURGE_UNCERTAIN_FENCE_REQUIRED")
        validate_fence_evidence(evidence, target, operation, job)
        codes = set(
            session.scalars(
                select(OperationEvent.code).where(
                    OperationEvent.operation_id == operation.id
                )
            ).all()
        )
        if not FENCE_EVENT_CODES <= codes or codes & FENCE_FORBIDDEN_EVENT_CODES:
            raise PurgeError("PURGE_FENCE_EVENTS_INVALID")
        operation_reports.append({"operation_id": operation.id, "fenced": True})

    active_jobs = session.scalars(
        select(Job).where(
            Job.operation_id.in_(
                select(Operation.id).where(Operation.instance_id.in_(identifiers))
            ),
            Job.status.in_({"queued", "running"}),
        )
    ).all()
    if active_jobs:
        raise PurgeError("PURGE_ACTIVE_JOB")

    active_apply = session.scalars(
        select(TerraformApply).where(
            TerraformApply.instance_id.in_(identifiers),
            TerraformApply.status.in_(ACTIVE_APPLY_STATUSES | {"uncertain"}),
        )
    ).all()
    if active_apply:
        raise PurgeError("PURGE_ACTIVE_TERRAFORM_APPLY")
    active_execution = session.scalars(
        select(TerraformExecution).where(
            TerraformExecution.instance_id.in_(identifiers),
            TerraformExecution.status.in_(ACTIVE_EXECUTION_STATUSES),
        )
    ).all()
    if active_execution:
        raise PurgeError("PURGE_ACTIVE_TERRAFORM_EXECUTION")
    active_power = session.scalars(
        select(PowerExecution).where(
            PowerExecution.instance_id.in_(identifiers),
            PowerExecution.status.in_(ACTIVE_POWER_STATUSES | {"uncertain"}),
        )
    ).all()
    if active_power:
        raise PurgeError("PURGE_ACTIVE_POWER_OPERATION")
    active_delete = session.scalars(
        select(DeletionExecution).where(
            DeletionExecution.instance_id.in_(identifiers),
            DeletionExecution.status.in_(ACTIVE_DELETE_STATUSES | {"uncertain"}),
        )
    ).all()
    if active_delete:
        raise PurgeError("PURGE_ACTIVE_DELETE_OPERATION")
    active_guest = session.scalars(
        select(GuestInitialization).where(
            GuestInitialization.instance_id.in_(identifiers),
            GuestInitialization.status.in_(ACTIVE_GUEST_STATUSES | {"uncertain"}),
        )
    ).all()
    if active_guest:
        raise PurgeError("PURGE_ACTIVE_GUEST_INITIALIZATION")

    locks = _advisory_locks(session)
    if locks:
        raise PurgeError("PURGE_ADVISORY_LOCK_PRESENT")
    return {
        "active_operations": operation_reports,
        "active_jobs": [],
        "advisory_locks": locks,
    }


def _list_object_versions(client, bucket: str, key: str) -> list[dict]:
    result = []
    pages = client.get_paginator("list_object_versions").paginate(
        Bucket=bucket, Prefix=key
    )
    for page in pages:
        for section, marker in (("Versions", False), ("DeleteMarkers", True)):
            for row in page.get(section, []):
                if row.get("Key") != key:
                    continue
                version_id = row.get("VersionId")
                if not isinstance(version_id, str) or not version_id:
                    raise PurgeError("PURGE_STATE_VERSION_ID_MISSING")
                result.append(
                    {
                        "key": key,
                        "version_id": version_id,
                        "delete_marker": marker,
                    }
                )
    return result


def inspect_state(target: PurgeTarget, env: dict) -> dict:
    backend = Backend.model_validate(target.allocation.get("backend"))
    reader = S3StateReader(backend.arguments(target.state_key), env)
    try:
        versions = _list_object_versions(reader.client, reader.bucket, reader.key)
        lock_versions = _list_object_versions(
            reader.client, reader.bucket, reader.key + ".tflock"
        )
        lock_present = False
        try:
            reader.client.head_object(Bucket=reader.bucket, Key=reader.key + ".tflock")
            lock_present = True
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
        if lock_present:
            raise PurgeError("PURGE_STATE_LOCK_PRESENT")

        current_present = True
        resource_count = 0
        metadata = None
        try:
            current = reader.read(lambda: False)
            latest = reader.read(lambda: False)
            if current.metadata() != latest.metadata():
                raise PurgeError("PURGE_STATE_CHANGED_DURING_AUDIT")
            metadata = current.metadata()
            resource_count = sum(
                len(resource.get("instances", []))
                for resource in current.state.get("resources", [])
                if resource.get("mode") == "managed"
            )
        except PlanError as failure:
            if failure.code != "STATE_BACKEND_OBJECT_MISSING":
                raise PurgeError("PURGE_STATE_READ_FAILED") from None
            current_present = False

        if resource_count:
            raise PurgeError("PURGE_STATE_RESOURCE_PRESENT")
        if current_present and not versions:
            raise PurgeError("PURGE_STATE_VERSION_INVENTORY_INCOMPLETE")
        return {
            "instance_id": target.instance_id,
            "vmid": target.vmid,
            "bucket": reader.bucket,
            "state_key": target.state_key,
            "state_key_sha256": target.public()["state_key_sha256"],
            "current_present": current_present,
            "resource_count": resource_count,
            "metadata": metadata,
            "state_versions": versions,
            "lock_versions": lock_versions,
            "version_count": len(versions),
            "lock_version_count": len(lock_versions),
        }
    finally:
        reader.close()


def delete_state_versions(inventory: dict, env: dict) -> None:
    # El endpoint y región proceden de la asignación técnica, nunca de la
    # entrada del cliente. Para borrar se vuelve a crear el cliente usando las
    # credenciales privadas del worker.
    import boto3
    from botocore.config import Config

    client = boto3.Session().client(
        "s3",
        endpoint_url=inventory["endpoint"],
        region_name=inventory["region"],
        aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=env.get("AWS_SESSION_TOKEN"),
        verify=env.get("SSL_CERT_FILE", True),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            proxies={},
            connect_timeout=5,
            read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": 2},
        ),
    )
    objects = inventory["state_versions"] + inventory["lock_versions"]
    for offset in range(0, len(objects), 1000):
        response = client.delete_objects(
            Bucket=inventory["bucket"],
            Delete={
                "Objects": [
                    {"Key": item["key"], "VersionId": item["version_id"]}
                    for item in objects[offset : offset + 1000]
                ],
                "Quiet": True,
            },
        )
        if response.get("Errors"):
            raise PurgeError("PURGE_STATE_DELETE_FAILED")
    remaining = _list_object_versions(client, inventory["bucket"], inventory["state_key"])
    remaining += _list_object_versions(
        client, inventory["bucket"], inventory["state_key"] + ".tflock"
    )
    if remaining:
        raise PurgeError("PURGE_STATE_DELETE_NOT_RECONCILED")


def _delete_rows(session, identifiers: list[str]) -> dict[str, int]:
    operation_ids = set(
        session.scalars(
            select(Operation.id).where(Operation.instance_id.in_(identifiers))
        ).all()
    )
    job_ids = set(
        session.scalars(
            select(Job.id).where(Job.operation_id.in_(operation_ids))
        ).all()
    ) if operation_ids else set()
    job_ids.update(
        session.scalars(
            select(TerraformExecution.job_id).where(
                TerraformExecution.instance_id.in_(identifiers)
            )
        ).all()
    )
    job_ids.update(
        session.scalars(
            select(TerraformApply.job_id).where(
                TerraformApply.instance_id.in_(identifiers),
                TerraformApply.job_id.is_not(None),
            )
        ).all()
    )
    apply_ids = set(
        session.scalars(
            select(TerraformApply.id).where(
                TerraformApply.instance_id.in_(identifiers)
            )
        ).all()
    )
    counts: dict[str, int] = {}

    def execute(name: str, statement) -> None:
        result = session.execute(statement)
        counts[name] = max(int(result.rowcount or 0), 0)

    execute(
        "power_executions",
        delete(PowerExecution).where(PowerExecution.instance_id.in_(identifiers)),
    )
    execute(
        "deletion_executions",
        delete(DeletionExecution).where(DeletionExecution.instance_id.in_(identifiers)),
    )
    execute(
        "guest_initializations",
        delete(GuestInitialization).where(GuestInitialization.instance_id.in_(identifiers)),
    )
    execute(
        "network_discoveries",
        delete(NetworkDiscovery).where(NetworkDiscovery.instance_id.in_(identifiers)),
    )
    execute(
        "operation_events",
        delete(OperationEvent).where(OperationEvent.operation_id.in_(operation_ids)),
    )
    certificate_filter = [PowerOwnershipCertificate.instance_id.in_(identifiers)]
    if apply_ids:
        certificate_filter.append(PowerOwnershipCertificate.approval_id.in_(apply_ids))
    execute(
        "power_ownership_certificates",
        delete(PowerOwnershipCertificate).where(or_(*certificate_filter)),
    )
    execute(
        "terraform_applies",
        delete(TerraformApply).where(TerraformApply.instance_id.in_(identifiers)),
    )
    execute(
        "terraform_attempts",
        delete(TerraformAttempt).where(TerraformAttempt.instance_id.in_(identifiers)),
    )
    execute(
        "terraform_executions",
        delete(TerraformExecution).where(TerraformExecution.instance_id.in_(identifiers)),
    )
    execute(
        "commercial_intents",
        delete(CommercialIntent).where(CommercialIntent.instance_id.in_(identifiers)),
    )
    execute(
        "technical_ownership",
        delete(TechnicalOwnership).where(TechnicalOwnership.instance_id.in_(identifiers)),
    )
    if job_ids:
        execute("jobs", delete(Job).where(Job.id.in_(job_ids)))
    else:
        counts["jobs"] = 0
    if operation_ids:
        execute("operations", delete(Operation).where(Operation.id.in_(operation_ids)))
    else:
        counts["operations"] = 0
    execute("instances", delete(Instance).where(Instance.id.in_(identifiers)))
    return counts


def run_purge(
    *,
    vmid_range: str,
    total_purge: bool,
    confirmation: str | None = None,
    execute: bool = False,
    fence_evidence: dict[str, dict] | None = None,
    session_factory=None,
    cluster_vmids_fn: Callable[[], set[int]] | None = None,
    runtime_idle_fn: Callable[[], dict] | None = None,
) -> dict:
    start, end = parse_vmid_range(vmid_range)
    if execute and not total_purge:
        raise PurgeError("TOTAL_PURGE_REQUIRED")
    if execute and total_purge:
        validate_total_purge_confirmation(start, end, confirmation)
    session_factory = session_factory or get_session_factory()
    cluster_vmids_fn = cluster_vmids_fn or cluster_vmids
    runtime_idle_fn = runtime_idle_fn or assert_broker_and_workers_idle
    fence_evidence = fence_evidence or {}

    with session_factory() as session:
        targets = collect_targets(session, start, end)
        protected = sorted({target.vmid for target in targets} & PROTECTED_VMIDS)
        if protected:
            raise PurgeError("PURGE_PROTECTED_VMID")
        physical = set(cluster_vmids_fn())
        occupied = sorted(physical & {target.vmid for target in targets})
        if occupied:
            raise PurgeError("PURGE_PHYSICAL_RESOURCE_PRESENT")
        activity = validate_database_activity(session, targets, fence_evidence)
        runtime = runtime_idle_fn()

    if not targets:
        return {
            "passed": True,
            "mode": "total-purge" if total_purge else "dry-run",
            "range": {"start": start, "end": end},
            "target_count": 0,
            "targets": [],
            "physical_occupied": [],
            "database_activity": activity,
            "runtime": runtime,
            "changed": False,
        }

    executor = build_executor(observation=True)
    with tempfile.TemporaryDirectory() as temporary:
        env = executor.environment(Path(temporary))
        inventories = []
        for target in targets:
            inventory = inspect_state(target, env)
            backend = Backend.model_validate(target.allocation.get("backend"))
            inventory["endpoint"] = backend.endpoint
            inventory["region"] = backend.region
            inventories.append(inventory)

        report = {
            "passed": True,
            "mode": "total-purge" if total_purge else "dry-run",
            "range": {"start": start, "end": end},
            "target_count": len(targets),
            "targets": [target.public() for target in targets],
            "physical_occupied": occupied,
            "database_activity": activity,
            "runtime": runtime,
            "state_inventory": [
                {
                    key: value
                    for key, value in inventory.items()
                    if key not in {"endpoint", "region"}
                }
                for inventory in inventories
            ],
            "changed": False,
        }
        if not execute:
            return report

        for inventory in inventories:
            delete_state_versions(inventory, env)

    identifiers = [target.instance_id for target in targets]
    with session_factory() as session:
        current_targets = collect_targets(session, start, end)
        if {item.instance_id for item in current_targets} != set(identifiers):
            raise PurgeError("PURGE_TARGET_SET_CHANGED")
        validate_database_activity(session, current_targets, fence_evidence)
        with admission_lock(session):
            locked_targets = session.scalars(
                select(Instance)
                .where(Instance.id.in_(identifiers))
                .with_for_update()
            ).all()
            if len(locked_targets) != len(identifiers) or any(
                instance.status != "deleted" for instance in locked_targets
            ):
                raise PurgeError("PURGE_INSTANCE_CHANGED")
            counts = _delete_rows(session, identifiers)
            session.commit()

    with session_factory() as session:
        remaining = collect_targets(session, start, end)
        if remaining:
            raise PurgeError("PURGE_DATABASE_NOT_RECONCILED")
    with tempfile.TemporaryDirectory() as temporary:
        env = executor.environment(Path(temporary))
        for inventory in inventories:
            target = next(
                item for item in targets if item.instance_id == inventory["instance_id"]
            )
            final_inventory = inspect_state(target, env)
            if final_inventory["state_versions"] or final_inventory["lock_versions"]:
                raise PurgeError("PURGE_STATE_NOT_RECONCILED")

    report.update({
        "changed": True,
        "deleted_rows": counts,
        "postconditions": {
            "database_targets_absent": True,
            "state_versions_absent": True,
            "locks_absent": True,
            "physical_targets_absent": True,
        },
    })
    return report


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise PurgeError("PURGE_REQUEST_INVALID")
        result = run_purge(**request)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except (PurgeError, PlanError, ClientError, BotoCoreError, OSError) as error:
        print(json.dumps({"passed": False, "reason": str(error)}))
        return 1
    except Exception as error:  # noqa: BLE001 - salida sanitizada para el operador
        print(json.dumps({"passed": False, "reason": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
