"""Disponibilidad conservadora por tenant. Consultar no reserva capacidad."""
from ..catalog import IMAGES, PLANS, resolve_image
from ..terraform.config import PlanError
from ..terraform.profiles import load_profile
from .lab import usage


def authorized_profile(tenant_id):
    try:
        profile = load_profile("lab-dhcp", tenant_id)
        image = resolve_image("ubuntu-2404")
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
    reasons.extend(["PHYSICAL_CAPACITY_NOT_VERIFIED", "ROOT_BOOTSTRAP_OUTSIDE_STATE_UNAVAILABLE"])
    return {"available": False, "capacity_verified": False, "reasons": reasons}


def plans_for(session, tenant_id):
    from dataclasses import asdict
    p = authorized_profile(tenant_id)
    used = usage(session, tenant_id)
    return [{**asdict(plan), "availability": plan_availability(plan, p, used)} for plan in PLANS]


def images_for(tenant_id):
    authorized = authorized_profile(tenant_id) is not None
    return [{"image_id": i.image_id, "operating_system": i.operating_system, "version": i.version,
             "status": i.status, "cloud_init": i.cloud_init, "qemu_guest_agent": i.qemu_guest_agent,
             "root_access": i.root_access, "ssh_access": i.ssh_access,
             "minimum_requirements": {"vcpus": i.minimum_vcpus, "memory_mb": i.minimum_memory_mb,
                                      "disk_gb": i.minimum_disk_gb},
             "compatible_plans": [p.plan_id for p in PLANS if p.vcpus >= i.minimum_vcpus
                 and p.memory_mb >= i.minimum_memory_mb and p.disk_gb >= i.minimum_disk_gb],
             "authorized": authorized, "availability": {"available": False, "capacity_verified": False,
                 "reasons": (["TENANT_PROFILE_NOT_AUTHORIZED"] if not authorized else [])
                    + ["ROOT_BOOTSTRAP_OUTSIDE_STATE_UNAVAILABLE", "PHYSICAL_CAPACITY_NOT_VERIFIED"]}}
            for i in IMAGES]
