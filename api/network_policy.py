"""Perfiles del piloto y bloqueo WAN explícito para la promoción definitiva."""

import os
from fastapi import HTTPException


def wan_only() -> bool:
    return os.getenv("IACP_ENFORCE_WAN_ONLY", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def selected_profile(requested: str | None = None) -> str:
    if wan_only():
        if requested not in {None, "prod_static"}:
            raise HTTPException(422, "PRODUCTION_REQUIRES_WAN")
        return "prod_static"
    profile = requested or os.getenv("IACP_DEV_NETWORK_PROFILE", "lab_dhcp").strip()
    if profile not in {"lab_dhcp", "prod_static"}:
        raise HTTPException(503, "NETWORK_POLICY_INVALID")
    return profile


def apply_to_spec(spec):
    from api.catalog import network_profile_by_id

    profile = selected_profile(None if wan_only() else spec.network_profile)
    if not network_profile_by_id(profile)["enabled"]:
        raise HTTPException(503, "NETWORK_PROFILE_NOT_CONFIGURED")
    spec.network_profile = profile
    spec.network_mode = "static" if profile == "prod_static" else "dhcp"
    spec.vlan_id = (
        network_profile_by_id(profile)["proxmox_vlan_tag"]
        if profile == "prod_static"
        else None
    )
    spec.ipv4_cidr = None
    spec.ipv4_gateway = None
    if profile == "prod_static":
        spec.dns_servers = ["1.1.1.1", "8.8.8.8"]
    return spec
