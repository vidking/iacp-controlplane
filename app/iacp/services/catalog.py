"""Disponibilidad conservadora por tenant. Consultar no reserva capacidad."""
import os
from ..catalog import IMAGES, PENDING_IMAGES, PLANS, resolve_image
from ..terraform.config import PlanError
from ..terraform.profiles import load_profile
from .lab import usage


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


def plan_availability(plan, profile, used):
    reasons = []
    if profile is None:
        reasons.append("TENANT_PROFILE_NOT_AUTHORIZED")
    else:
        if (plan.vcpus > profile.max_vcpus or plan.memory_mb > profile.max_memory_mb
                or plan.disk_gb > profile.max_disk_gb):
            reasons.append("ENVIRONMENT_PLAN_LIMIT_EXCEEDED")
        if plan.disk_gb < profile.settings.boot_disk.minimum_size_gb:
            reasons.append("DISK_SHRINK_FORBIDDEN")
        if (used["instances"] + 1 > profile.max_live_instances
                or used["vcpus"] + plan.vcpus > profile.max_total_vcpus
                or used["memory_mb"] + plan.memory_mb > profile.max_total_memory_mb
                or used["disk_gb"] + plan.disk_gb > profile.max_total_disk_gb
                or used["concurrent_operations"] + 1 > min(2, profile.max_concurrent_operations)):
            reasons.append("TENANT_TECHNICAL_QUOTA_EXCEEDED")
    # No convertir un catálogo estático en un certificado de capacidad física.
    reasons.extend(["PHYSICAL_CAPACITY_NOT_VERIFIED", "COMMERCIAL_CONTRACT_NOT_ACTIVE"])
    return {"available": False, "capacity_verified": False, "reasons": reasons}


def plans_for(session, tenant_id):
    from dataclasses import asdict
    p = authorized_profile(tenant_id)
    used = usage(session, tenant_id)
    result=[]
    for plan in PLANS:
        availability=plan_availability(plan,p,used)
        if os.environ.get('IACP_CREATION_CONTRACT')=='commercial-g':
            from .commercial import limits_for,quota,live_capacity
            from .bootstrap_secrets import configured_key,BootstrapSecretError
            from .instances import Conflict
            # G usa derechos y presupuesto explícitos del tenant. Los topes del
            # perfil histórico no definen ni reducen los productos comerciales.
            reasons=[]
            verified=False
            try:
                limits=limits_for(tenant_id)
                if plan.plan_id not in limits.allowed_plans:raise Conflict('COMMERCIAL_PLAN_NOT_AUTHORIZED')
                quota(session,tenant_id,limits,asdict(plan))
                configured_key()
                if p is None:raise Conflict('COMMERCIAL_PROFILE_NOT_AUTHORIZED')
                if plan.disk_gb < p.settings.boot_disk.minimum_size_gb:raise Conflict('DISK_SHRINK_FORBIDDEN')
                if p.root_password_bootstrap:raise Conflict('COMMERCIAL_LEGACY_PASSWORD_PROFILE_FORBIDDEN')
                if p.guest_initialization!='ubuntu-2404-lab-identity-v1':raise Conflict('COMMERCIAL_GUEST_INITIALIZATION_REQUIRED')
                live_capacity(session,p,plan);verified=True
            except (Conflict,BootstrapSecretError) as error:reasons.append(str(error))
            availability={'available':not reasons,'capacity_verified':verified,'reasons':reasons}
        result.append({**asdict(plan),'availability':availability})
    return result


def images_for(tenant_id,session=None):
    available_plans=(plans_for(session,tenant_id) if session is not None
        and os.environ.get('IACP_CREATION_CONTRACT')=='commercial-g' else [])
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
