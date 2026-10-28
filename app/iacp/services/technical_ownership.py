"""Autorización técnica única para todas las KVM administradas por IACP.

La autorización se deriva de la evidencia de aprovisionamiento persistida.  No
consulta LabPolicy, cuotas comerciales, expiraciones ni el pin del release
actual.  ``adopt=True`` solo materializa una fila cuando toda la evidencia ya
existía; nunca ejecuta Terraform ni muta Proxmox.
"""

from types import SimpleNamespace

from sqlalchemy import select

from ..models import (
    Instance,
    TechnicalOwnership,
    TerraformApply,
    TerraformExecution,
    Tenant,
    utcnow,
)
from .instances import Conflict
from .ownership_binding import binding_digest, valid_source_revision

TECHNICAL_ACTIONS = frozenset({"create", "start", "shutdown", "reboot", "delete"})
PROTECTED_VMIDS = frozenset({104, 106, 29000})


def _completed_apply(session, execution):
    if execution is None or execution.plan_sha256 is None:
        return None
    return session.scalar(select(TerraformApply).where(
        TerraformApply.instance_id == execution.instance_id,
        TerraformApply.attempt == execution.attempt,
        TerraformApply.plan_sha256 == execution.plan_sha256,
        TerraformApply.status == "completed",
    ))


def _evidence(instance, execution, apply):
    result = apply.result if isinstance(apply.result, dict) else {}
    observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
    if (
        instance.executor != "terraform"
        or instance.status == "deleted"
        or not execution.state_key
        or not valid_source_revision(execution.source_revision)
        or execution.vmid is None
        or execution.vmid < 30000
        or execution.vmid in PROTECTED_VMIDS
        or instance.vmid != execution.vmid
        or instance.node != result.get("node")
        or instance.vmid != result.get("vmid")
        or instance.name != result.get("name")
        or result.get("simulated") is not False
        or observation.get("postconditions_verified") is not True
        or observation.get("state_present") is not True
        or observation.get("proxmox_present") is not True
    ):
        raise Conflict("TECHNICAL_OWNERSHIP_EVIDENCE_REQUIRED")
    if not isinstance(execution.allocation, dict):
        raise Conflict("TECHNICAL_OWNERSHIP_EVIDENCE_REQUIRED")
    allocation = execution.allocation
    if (
        allocation.get("vm_id") != execution.vmid
        or allocation.get("node_name") != instance.node
        or allocation.get("image") != instance.image
    ):
        raise Conflict("TECHNICAL_OWNERSHIP_EVIDENCE_REQUIRED")
    return allocation


def _snapshot(session, instance, execution):
    # La asignación sellada es la compatibilidad efectiva de la instancia.
    # Preferirla evita que un cambio del catálogo actual reinterprete una VM
    # histórica como si hubiera sido creada por otro perfil.
    if isinstance(execution.allocation, dict) and execution.allocation.get("image"):
        return dict(execution.allocation)
    if not instance.network_profile:
        return {}
    try:
        from ..terraform.profiles import load_profile

        profile = load_profile(instance.network_profile, instance.tenant_id, image_id=instance.image)
        snapshot = profile.model_dump(mode="json")
        if snapshot.get("guest_initialization"):
            try:
                from .guest_identity import program_for_mode

                snapshot["guest_initialization_program_sha256"] = program_for_mode(
                    snapshot["guest_initialization"],
                    root=bool(snapshot.get("root_password_bootstrap")),
                )[1]
            except Exception:
                pass
        return snapshot
    except Exception:
        # A historical source revision remains administrable from its sealed
        # allocation even when the current profile catalog has moved on.
        return {"network_profile": instance.network_profile,
                "allocation": execution.allocation or {}}


def _membership(session, instance, ownership, execution, *, current_profiles=False):
    snapshot = _snapshot(session, instance, execution)
    return SimpleNamespace(
        id="technical:" + ownership.binding_sha256,
        binding_sha256=ownership.binding_sha256,
        profile_snapshots={instance.network_profile: snapshot} if instance.network_profile else {},
        specification={"actions": sorted(TECHNICAL_ACTIONS)},
        revision="provisioning-api-core",
    )


def record(session, instance, *, adopt=False, current_profiles=False):
    """Validate or idempotently materialize the technical ownership row."""

    tenant = session.get(Tenant, instance.tenant_id)
    execution = session.get(TerraformExecution, instance.id)
    apply = _completed_apply(session, execution)
    if tenant is None or not tenant.active or apply is None:
        raise Conflict("TECHNICAL_OWNERSHIP_EVIDENCE_REQUIRED")
    allocation = _evidence(instance, execution, apply)
    digest = binding_digest(instance, execution, apply)
    ownership = session.get(TechnicalOwnership, instance.id)
    if ownership is not None:
        if (
            ownership.tenant_id != instance.tenant_id
            or ownership.vmid != execution.vmid
            or ownership.node != instance.node
            or ownership.name != instance.name
            or ownership.state_key != execution.state_key
            or ownership.source_revision != execution.source_revision
            or ownership.binding_sha256 != digest
        ):
            raise Conflict("TECHNICAL_OWNERSHIP_BINDING_CHANGED")
    elif adopt:
        ownership = TechnicalOwnership(
            instance_id=instance.id,
            tenant_id=instance.tenant_id,
            vmid=execution.vmid,
            node=instance.node,
            name=instance.name,
            state_key=execution.state_key,
            source_revision=execution.source_revision,
            binding_sha256=digest,
            allocation=allocation,
            evidence={
                "apply_id": apply.id,
                "attempt": apply.attempt,
                "plan_sha256": apply.plan_sha256,
                "observed_at": (instance.result or {}).get("observed_at"),
            },
            verified_at=utcnow(),
        )
        session.add(ownership)
        session.flush()
    else:
        raise Conflict("TECHNICAL_OWNERSHIP_REQUIRED")
    return ownership


def _precreate_member(session, instance):
    """Authorize planning/apply before a physical KVM exists."""

    tenant = session.get(Tenant, instance.tenant_id)
    execution = session.get(TerraformExecution, instance.id)
    if (
        tenant is None or not tenant.active or instance.status == "deleted"
        or instance.executor != "terraform" or execution is None
        or not execution.state_key or not valid_source_revision(execution.source_revision)
        or execution.vmid is None or execution.vmid < 30000
        or execution.vmid in PROTECTED_VMIDS
    ):
        raise Conflict("TECHNICAL_OWNERSHIP_EVIDENCE_REQUIRED")
    return SimpleNamespace(
        id="technical:pending:" + instance.id,
        binding_sha256=None,
        profile_snapshots={instance.network_profile: _snapshot(session, instance, execution)}
        if instance.network_profile else {},
        specification={"actions": sorted(TECHNICAL_ACTIONS)},
        revision="provisioning-api-core",
    )


def member(session, instance, action, *, current_profiles=False, observation=False, adopt=False):
    if action not in TECHNICAL_ACTIONS:
        raise Conflict("TECHNICAL_ACTION_NOT_SUPPORTED")
    execution = session.get(TerraformExecution, instance.id)
    if action == "create" and _completed_apply(session, execution) is None:
        return _precreate_member(session, instance)
    ownership = record(session, instance, adopt=adopt, current_profiles=current_profiles)
    return _membership(session, instance, ownership, execution, current_profiles=current_profiles)


def binding(session, instance, *, adopt=False):
    ownership = record(session, instance, adopt=adopt)
    return ownership.binding_sha256

