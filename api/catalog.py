"""Versioned MNG catalog used by the public API and the worker.

The catalog is deliberately kept in the Terraform environment directory so
the API, the worker and the generated Terraform variables all resolve the
same plan and Golden Image identifiers.
"""

import json
import os
from pathlib import Path


def catalog_path() -> Path:
    default_path = Path(__file__).resolve().parent.parent / "terraform" / "environments" / "mng01" / "catalog.json"
    return Path(os.getenv("IACP_MNG_CATALOG", str(default_path)))


def load_catalog() -> dict:
    with catalog_path().open(encoding="utf-8") as handle:
        return json.load(handle)


def plan_catalog() -> list[dict]:
    return list(load_catalog().get("plans", []))


def image_catalog() -> list[dict]:
    return list(load_catalog().get("images", []))


def cluster_nodes() -> list[str]:
    catalog = load_catalog()
    nodes = catalog.get("nodes") or [catalog.get("node", "mng01")]
    return [str(node) for node in nodes if str(node).strip()]


def plan_by_id(plan_id: str) -> dict | None:
    return next((plan for plan in plan_catalog() if plan.get("plan_id") == plan_id), None)


def image_by_id(image_id: str) -> dict | None:
    return next((image for image in image_catalog() if image.get("image_id") == image_id), None)


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def network_profile_catalog() -> list[dict]:
    """Return the stable network profiles without exposing NetBox secrets."""

    # The profile stays disabled until the deployment opts in and supplies
    # the exact authorized /24. Secrets are intentionally not exposed here;
    # the worker validates the token when it performs an allocation.
    try:
        prefix_id = int((os.getenv("IACP_NETBOX_PREFIX_ID") or "").strip())
        prefix_valid = prefix_id > 0
        prefix_valid = prefix_valid and int(os.getenv("IACP_NETBOX_PREFIX_LENGTH", "24")) == 24
        prefix_valid = prefix_valid and int(os.getenv("IACP_NETBOX_GATEWAY_HOST", "1")) == 1
    except ValueError:
        prefix_valid = False
    prod_enabled = (
        _enabled(os.getenv("IACP_PROD_STATIC_ENABLED"))
        and bool((os.getenv("IACP_NETBOX_URL") or "").strip())
        and prefix_valid
    )
    return [
        {
            "profile_id": "lab_dhcp",
            "label": "Lab_DHCP",
            "network_mode": "dhcp",
            "ipam": None,
            "enabled": True,
        },
        {
            "profile_id": "prod_static",
            "label": "Prod_Static",
            "network_mode": "static",
            "ipam": "netbox",
            "prefix_length": 24,
            "gateway_policy": "first-usable-x.x.x.1",
            "enabled": prod_enabled,
        },
    ]


def network_profile_by_id(profile_id: str) -> dict | None:
    return next((profile for profile in network_profile_catalog() if profile["profile_id"] == profile_id), None)
