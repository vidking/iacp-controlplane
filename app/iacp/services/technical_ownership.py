"""Autorización técnica única para todas las KVM administradas por IACP.

La autorización se deriva de la evidencia de aprovisionamiento persistida.  No
consulta LabPolicy, cuotas comerciales, expiraciones ni el pin del release
actual.  ``adopt=True`` solo materializa una fila cuando toda la evidencia ya
existía; nunca ejecuta Terraform ni muta Proxmox.
"""

from types import SimpleNamespace

from sqlalchemy import select

from ..models import (
    CommercialIntent,
    GuestInitialization,
    Instance,
    TechnicalOwnership,
    TerraformApply,
    TerraformExecution,
    Tenant,
    utcnow,
)
from .instances import Conflict
from .ownership_binding import binding_digest, valid_source_revision
from ..terraform.config import PlanError

TECHNICAL_ACTIONS = frozenset({"create", "start", "shutdown", "reboot", "delete"})
# Recursos que nunca pueden ser administrados como KVM de tenant.
# VM 106 es una KVM histórica de laboratorio, no una imagen ni el Control
# Plane; su administración depende de ownership técnico verificable.
PROTECTED_VMIDS = frozenset({104, 29000, 29001, 29010, 29011})
HISTORICAL_MANAGED_VMIDS = frozenset({106})


def is_managed_vmid(vmid):
    """Indica si un VMID puede tener ownership técnico administrable."""
    return vmid in HISTORICAL_MANAGED_VMIDS or (
        isinstance(vmid, int) and vmid >= 30000 and vmid not in PROTECTED_VMIDS
    )


def is_new_vmid(vmid):
    """Indica si el VMID es válido para una creación nueva.

    Las KVM históricas como 106 pueden administrarse, pero no pueden usarse
    como destino de una nueva intención CREATE.
    """
    return isinstance(vmid, int) and vmid >= 30000 and vmid not in PROTECTED_VMIDS


def _completed_apply(session, execution):
    if execution is None or execution.plan_sha256 is None:
        return None
    # El worker marca el apply como completed en la misma sesión antes de
    # materializar ownership. El proyecto usa autoflush=False; por eso el
    # estado se comprueba sobre la entidad cargada, después de filtrar por
    # identidad, en lugar de incluirlo en el WHERE contra una fila aún no
    # flush-eada.
    result = session.scalar(select(TerraformApply).where(
        TerraformApply.instance_id == execution.instance_id,
        TerraformApply.attempt == execution.attempt,
        TerraformApply.plan_sha256 == execution.plan_sha256,
    ))
    return result if result is not None and result.status == "completed" else None


def _evidence(instance, execution, apply):
    result = apply.result if isinstance(apply.result, dict) else {}
    observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
    if (
        instance.executor != "terraform"
        or instance.status == "deleted"
        or not execution.state_key
        or not valid_source_revision(execution.source_revision)
        or not is_managed_vmid(execution.vmid)
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


def _allocation_matches_profile(instance, allocation, profile):
    """Comprueba que un snapshot de perfil describe la asignación sellada."""
    if not isinstance(allocation, dict) or not isinstance(profile, dict):
        return False
    settings = profile.get("settings") if isinstance(profile.get("settings"), dict) else {}
    if settings.get("image") != instance.image:
        return False
    for key in ("node_name", "template_vm_id", "template_node_name", "network_mode",
                "network_bridge", "vlan_id", "clone_datastore_id", "cloudinit_datastore_id"):
        if key in allocation and settings.get(key) != allocation.get(key):
            return False
    allocation_disk = allocation.get("boot_disk")
    profile_disk = settings.get("boot_disk")
    if isinstance(allocation_disk, dict) and isinstance(profile_disk, dict):
        if allocation_disk.get("datastore_id") != profile_disk.get("datastore_id"):
            return False
    return instance.tenant_id in profile.get("allowed_tenants", [])


def _profile_from_intent(session, instance, allocation):
    """Recupera el snapshot técnico persistido en la intención, si es íntegro."""
    intent = session.get(CommercialIntent, instance.id)
    specification = intent.specification if intent and isinstance(intent.specification, dict) else {}
    raw = specification.get("profile_snapshot")
    if not isinstance(raw, dict):
        return None
    try:
        from ..terraform.profiles import Profile

        profile = Profile.model_validate(raw)
        snapshot = profile.model_dump(mode="json")
    except (PlanError, TypeError, ValueError):
        return None
    return snapshot if _allocation_matches_profile(instance, allocation, snapshot) else None


def _profile_from_current_catalog(session, instance, allocation):
    """Usa el catálogo actual solo si todavía describe exactamente el recurso."""
    if not instance.network_profile:
        return None
    try:
        from ..terraform.profiles import Profile, load_profile

        profile = load_profile(instance.network_profile, instance.tenant_id, image_id=instance.image)
        snapshot = Profile.model_validate(profile.model_dump(mode="json")).model_dump(mode="json")
    except (PlanError, TypeError, ValueError):
        return None
    return snapshot if _allocation_matches_profile(instance, allocation, snapshot) else None


def _add_initialization_evidence(session, instance, snapshot):
    """Añade evidencia persistida; nunca reabre ni reentrega guest-exec."""
    initialization = session.scalar(select(GuestInitialization).where(
        GuestInitialization.instance_id == instance.id))
    if initialization is None:
        return snapshot
    snapshot = dict(snapshot)
    snapshot.setdefault("guest_initialization", initialization.mode)
    # La huella observada prevalece sobre una inferencia del catálogo. Si no
    # coincide con el programa vigente, configured_mode() fallará cerrado.
    snapshot["guest_initialization_program_sha256"] = initialization.program_sha256
    from .guest_identity import ROOT_PROGRAM_SHA256

    if initialization.program_sha256 == ROOT_PROGRAM_SHA256:
        snapshot["root_password_bootstrap"] = True
    snapshot["guest_initialization_evidence"] = {
        "operation_id": initialization.operation_id,
        "status": initialization.status,
        "program_sha256": initialization.program_sha256,
        "started_at": initialization.started_at.isoformat() if initialization.started_at else None,
        "finished_at": initialization.finished_at.isoformat() if initialization.finished_at else None,
    }
    return snapshot


def _snapshot(session, instance, execution):
    # Allocation conserva hardware, VMID, nodo y state key. El perfil técnico
    # aporta guest initialization y capacidades que no forman parte de la
    # Allocation. Nunca se sustituye una revisión histórica por la actual sin
    # comprobar que describe exactamente la misma asignación.
    allocation = dict(execution.allocation) if isinstance(execution.allocation, dict) else {}
    snapshot = _profile_from_intent(session, instance, allocation)
    if snapshot is None:
        snapshot = _profile_from_current_catalog(session, instance, allocation)
    if snapshot is None:
        snapshot = {}
    snapshot.update(allocation)
    if snapshot.get("guest_initialization") and not snapshot.get("guest_initialization_program_sha256"):
        try:
            from .contracts import instance_uses_technical_access
            from .guest_identity import program_for_mode, technical_program_for_mode

            if instance_uses_technical_access(instance):
                snapshot["guest_initialization_program_sha256"] = technical_program_for_mode(
                    snapshot["guest_initialization"])[1]
            else:
                snapshot["guest_initialization_program_sha256"] = program_for_mode(
                    snapshot["guest_initialization"],
                    root=bool(snapshot.get("root_password_bootstrap")),
                )[1]
        except (Conflict, ValueError):
            # configured_mode() will reject an incomplete or incompatible
            # historical profile; never invent a program digest.
            pass
    return _add_initialization_evidence(session, instance, snapshot)


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
        or not is_new_vmid(execution.vmid)
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

