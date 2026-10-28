"""Disponibilidad técnica. Consultar no reserva capacidad ni aplica cuotas."""
from ..catalog import IMAGES, PENDING_IMAGES, PLANS, resolve_image
from ..terraform.config import PlanError
from ..terraform.profiles import load_profile
from .contracts import is_managed_creation_contract


def authorized_profile(tenant_id, image_id="ubuntu-2404"):
    try:
        profile = load_profile("lab-dhcp", tenant_id, image_id=image_id)
        image = resolve_image(image_id)
    except (PlanError, ValueError):
        return None
    settings = profile.settings
    if (settings.environment != "lab" or settings.image != image.image_id
            or settings.template_vm_id != image.template_vm_id
            or (settings.template_node_name or settings.node_name) != image.source_node
            or settings.clone_datastore_id != image.source_storage
            or settings.boot_disk.datastore_id != image.source_storage
            or settings.cloudinit_datastore_id != image.source_storage
            or settings.network_mode != "dhcp" or settings.network_bridge != "vmbr0"
            or settings.vlan_id is not None or not settings.guest_agent_enabled):
        return None
    return profile


def plan_availability(plan, profile, used=None):
    """Return static technical checks before live-capacity verification.

    ``used`` is retained in the signature for callers of the historical
    catalog helper.  Persisted instance counts are deliberately not used as
    quotas; only the live capacity read performed by ``plans_for`` is
    authoritative for admission.
    """
    reasons = []
    if profile is None:
        reasons.append("TENANT_PROFILE_NOT_AUTHORIZED")
    else:
        if (plan.vcpus > profile.max_vcpus or plan.memory_mb > profile.max_memory_mb
                or plan.disk_gb > profile.max_disk_gb):
            reasons.append("ENVIRONMENT_PLAN_LIMIT_EXCEEDED")
        if plan.disk_gb < profile.settings.boot_disk.minimum_size_gb:
            reasons.append("DISK_SHRINK_FORBIDDEN")
    # No convertir un catálogo estático en un certificado de capacidad física.
    reasons.append("PHYSICAL_CAPACITY_NOT_VERIFIED")
    return {"available": False, "capacity_verified": False, "reasons": reasons}


def plans_for(session, tenant_id):
    from dataclasses import asdict
    p = authorized_profile(tenant_id)
    result=[]
    for plan in PLANS:
        availability=plan_availability(plan,p)
        from .commercial import live_capacity
        from .bootstrap_secrets import configured_key, BootstrapSecretError
        from .instances import Conflict
        reasons=[]; verified=False
        try:
            configured_key()
            if p is None: raise Conflict('PROVISIONING_PROFILE_NOT_AUTHORIZED')
            if plan.vcpus > p.max_vcpus or plan.memory_mb > p.max_memory_mb or plan.disk_gb > p.max_disk_gb:
                raise Conflict('ENVIRONMENT_PLAN_LIMIT_EXCEEDED')
            if plan.disk_gb < p.settings.boot_disk.minimum_size_gb:
                raise Conflict('DISK_SHRINK_FORBIDDEN')
            if p.root_password_bootstrap:
                raise Conflict('PROVISIONING_LEGACY_PASSWORD_PROFILE_FORBIDDEN')
            if not p.guest_initialization or not p.guest_initialization.endswith('-lab-identity-v1'):
                raise Conflict('PROVISIONING_GUEST_INITIALIZATION_REQUIRED')
            live_capacity(session,p,plan); verified=True
        except (Conflict, BootstrapSecretError) as error:
            reasons.append(str(error))
        availability={'available':not reasons,'capacity_verified':verified,'reasons':reasons}
        result.append({**asdict(plan),'availability':availability})
    return result


def images_for(tenant_id,session=None):
    available_plans=(plans_for(session,tenant_id) if session is not None
        and is_managed_creation_contract() else [])
    result=[]
    for image in IMAGES:
        profile=authorized_profile(tenant_id,image.image_id)
        authorized=profile is not None
        compatible=[plan.plan_id for plan in PLANS if plan.vcpus>=image.minimum_vcpus
            and plan.memory_mb>=image.minimum_memory_mb and plan.disk_gb>=image.minimum_disk_gb]
        available=authorized and any(plan['plan_id'] in compatible
            and plan['availability']['available'] for plan in available_plans)
        reasons=[]
        if not authorized:reasons.append('TENANT_PROFILE_NOT_AUTHORIZED')
        elif not available:reasons.append('PHYSICAL_CAPACITY_NOT_VERIFIED')
        result.append({'image_id':image.image_id,'operating_system':image.operating_system,
            'version':image.version,'status':image.status,'cloud_init':image.cloud_init,
            'qemu_guest_agent':image.qemu_guest_agent,'root_access':image.root_access,
            'ssh_access':image.ssh_access,'minimum_requirements':{'vcpus':image.minimum_vcpus,
                'memory_mb':image.minimum_memory_mb,'disk_gb':image.minimum_disk_gb},
            'compatible_plans':compatible,'authorized':authorized,
            'availability':{'available':available,'capacity_verified':available,'reasons':reasons}})
    return result
