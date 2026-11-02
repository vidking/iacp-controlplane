"""Catálogo técnico por entorno, compartido por la API y los workers."""

from __future__ import annotations

import json
from pathlib import Path

from api.targets import catalog_path as target_catalog_path


def catalog_path(environment: str | None = None) -> Path:
    return target_catalog_path(environment)


def load_catalog(environment: str | None = None) -> dict:
    with catalog_path(environment).open(encoding="utf-8") as handle:
        return json.load(handle)


def plan_catalog(environment: str | None = None) -> list[dict]:
    fields = ("plan_id", "label", "vcpus", "memory_mb", "disk_gb")
    return [
        {key: plan[key] for key in fields if key in plan}
        for plan in load_catalog(environment).get("plans", [])
    ]


def image_catalog(environment: str | None = None) -> list[dict]:
    fields = (
        "image_id",
        "label",
        "template_vm_id",
        "minimum_vcpus",
        "minimum_memory_mb",
        "minimum_disk_gb",
    )
    return [
        {key: image[key] for key in fields if key in image}
        for image in load_catalog(environment).get("images", [])
    ]


def cluster_nodes(environment: str | None = None) -> list[str]:
    selected = environment or "lab"
    catalog = load_catalog(selected)
    nodes = catalog.get("nodes") or [catalog.get("node", "mng01")]
    return [str(node) for node in nodes if str(node).strip()]


def plan_by_id(plan_id: str, environment: str | None = None) -> dict | None:
    return next((plan for plan in plan_catalog(environment) if plan.get("plan_id") == plan_id), None)


def image_by_id(image_id: str, environment: str | None = None) -> dict | None:
    return next((image for image in image_catalog(environment) if image.get("image_id") == image_id), None)


def network_profile_catalog() -> list[dict]:
    return [
        {
            "profile_id": "lab_dhcp",
            "label": "DHCP",
            "network_mode": "dhcp",
            "ipam": None,
            "enabled": True,
        }
    ]


def network_profile_by_id(profile_id: str) -> dict | None:
    return next((item for item in network_profile_catalog() if item["profile_id"] == profile_id), None)
