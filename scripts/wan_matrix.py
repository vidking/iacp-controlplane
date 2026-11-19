#!/usr/bin/env python3
"""Matriz física de red para servicios; producción requiere --production explícito.

La evidencia no contiene credenciales ni salidas de aplicación sin filtrar.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import html
import http.cookiejar
import json
import os
import random
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from html.parser import HTMLParser
from ipaddress import ip_address, ip_network
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.envfile import read_env_file
from api.catalog import (
    PROD_STATIC_FIRST_HOST,
    PROD_STATIC_LAST_HOST,
    PROD_STATIC_PREFIX,
    image_by_id,
    plan_by_id,
)
from worker.netbox import NetBoxClient, NetBoxError
from worker.proxmox import ProxmoxClient

WAN_BRIDGE = "vmbr3"
REQUIRED_SERVICE_WORKERS = 4
EXPECTED_ALMALINUX_IMAGES = {"almalinux-9": 29010, "almalinux-10": 29011}
EXCLUDED_WAN_ADDRESSES = {"190.0.231.2"}


def required_cycles(category):
    """KVM certification repeats each of its 16 combinations four times."""
    if category == "kvm":
        return 4
    if category == "services":
        return 4
    if category == "agents":
        return 2
    raise ValueError("MATRIX_CATEGORY_INVALID")


def wan_nic_config(env):
    """Validate the bridge authorized for this physical matrix."""

    bridge = (env.get("IACP_PROD_STATIC_BRIDGE") or "").strip()
    if bridge != WAN_BRIDGE:
        raise RuntimeError("WAN_BRIDGE_NOT_AUTHORIZED")
    return WAN_BRIDGE


def require_four_idle_service_workers(service_state):
    workers = service_state.get("workers") or []
    if (
        service_state.get("max_concurrent") != REQUIRED_SERVICE_WORKERS
        or len(workers) != REQUIRED_SERVICE_WORKERS
    ):
        raise RuntimeError("FOUR_SERVICE_WORKERS_REQUIRED")
    now = datetime.now(UTC)
    for worker in workers:
        try:
            heartbeat = datetime.fromisoformat(worker["last_heartbeat"])
            heartbeat_age = (now - heartbeat).total_seconds()
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("SERVICE_WORKERS_NOT_IDLE") from None
        if (
            worker.get("status") != "idle"
            or worker.get("job_id")
            or not 0 <= heartbeat_age < 60
        ):
            raise RuntimeError("SERVICE_WORKERS_NOT_IDLE")


def require_expected_golden_images(images, inventory, vm_config):
    configured = {
        str(row.get("image_id")): row.get("template_vm_id")
        for row in images
        if isinstance(row, dict)
    }
    if any(
        configured.get(image_id) != vmid
        for image_id, vmid in EXPECTED_ALMALINUX_IMAGES.items()
    ):
        raise RuntimeError("GOLDEN_IMAGE_CATALOG_MISMATCH")
    nodes = {
        int(row["vmid"]): str(row["node"])
        for row in inventory
        if isinstance(row, dict)
        and str(row.get("vmid", "")).isdigit()
        and row.get("node")
    }
    for vmid in EXPECTED_ALMALINUX_IMAGES.values():
        node = nodes.get(vmid)
        if node is None:
            raise RuntimeError("GOLDEN_IMAGE_NOT_READY")
        config = vm_config(node, vmid)
        if config.get("template") not in {1, "1", True}:
            raise RuntimeError("GOLDEN_IMAGE_NOT_TEMPLATE")
    return [
        {"image_id": image_id, "template_vm_id": vmid}
        for image_id, vmid in EXPECTED_ALMALINUX_IMAGES.items()
    ]


def verify_netbox_state(client):
    if client.config.expected_prefix != PROD_STATIC_PREFIX:
        raise RuntimeError("NETBOX_PREFIX_NOT_AUTHORIZED")
    required_exclusions = {ip_address(value) for value in EXCLUDED_WAN_ADDRESSES}
    if not required_exclusions.issubset(client.config.excluded_addresses):
        raise RuntimeError("NETBOX_REQUIRED_ADDRESS_EXCLUSION_MISSING")
    prefix = ip_network(PROD_STATIC_PREFIX)
    gateway = client.validate_gateway(prefix)
    if gateway != "190.0.231.1":
        raise RuntimeError("NETBOX_GATEWAY_NOT_AUTHORIZED")
    available = sorted(
        client.available_addresses(prefix), key=lambda item: int(item[0])
    )
    first_authorized = prefix.network_address + PROD_STATIC_FIRST_HOST
    last_authorized = prefix.network_address + PROD_STATIC_LAST_HOST
    eligible = [
        (address, length)
        for address, length in available
        if first_authorized <= address <= last_authorized and length == prefix.prefixlen
        and str(address) not in EXCLUDED_WAN_ADDRESSES
    ]
    if len(eligible) < 6:
        raise RuntimeError("NETBOX_AUTHORIZED_RANGE_EXHAUSTED")
    return {
        "prefix": str(prefix),
        "gateway": "190.0.231.1",
        "available_count": len(eligible),
        "next_available": f"{eligible[0][0]}/{eligible[0][1]}",
    }


def require_kvm_capacity(nodes, storage, *, memory_gib=48, disk_gib=1200):
    """Keep a reserve above the heaviest six worker Fedora/Debian batch."""

    node = next(
        (row for row in nodes if isinstance(row, dict) and row.get("node") == "mng01"),
        None,
    )
    if not node or node.get("status") != "online":
        raise RuntimeError("KVM_TARGET_NODE_NOT_READY")
    try:
        free_memory = int(node["maxmem"]) - int(node["mem"])
        available_storage = int(storage["avail"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("KVM_CAPACITY_UNAVAILABLE") from None
    required_memory = memory_gib * 1024**3
    required_storage = disk_gib * 1024**3
    if free_memory < required_memory:
        raise RuntimeError("KVM_MEMORY_CAPACITY_INSUFFICIENT")
    if available_storage < required_storage:
        raise RuntimeError("KVM_STORAGE_CAPACITY_INSUFFICIENT")
    return {
        "node": "mng01",
        "free_memory_gib": round(free_memory / 1024**3, 2),
        "required_memory_gib": memory_gib,
        "available_storage_gib": round(available_storage / 1024**3, 2),
        "required_storage_gib": disk_gib,
    }


def cases(category, images=None, cycle=1, *, include_controls=True):
    if category == "kvm":
        supported_images = [
            "ubuntu-2404",
            "ubuntu-2604",
            "almalinux-9",
            "almalinux-10",
            "fedora-45",
            "debian-13",
        ]
        default_images = supported_images[:-2]
    elif category == "services":
        supported_images = ["ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10"]
        default_images = supported_images
    else:
        supported_images = ["ubuntu-2404", "almalinux-9"]
        default_images = supported_images
    selected_images = list(dict.fromkeys(images or default_images))
    if not selected_images or set(selected_images) - set(supported_images):
        raise ValueError("MATRIX_IMAGES_INVALID")
    if category == "services" and cycle not in {1, 2, 3, 4}:
        raise ValueError("MATRIX_CYCLE_INVALID")
    if category == "services" and len(selected_images) == 4:
        selected_images = selected_images[:2] if cycle == 1 else selected_images[2:]
    products = (
        ["n8n", "nextcloud"]
        if category == "services" and not include_controls
        else ["kvm"]
        if category == "kvm"
        else ["nextcloud", "n8n"]
        if category == "services"
        else ["hermes", "openclaw"]
    )
    items = [(product, image) for product in products for image in selected_images]
    result = [
        {"product": product, "image": image, "plan": f"kvm-{plan}", "control": False}
        for product, image in items
        for plan in range(1, 5)
    ]
    if not include_controls:
        if not result:
            raise ValueError("MATRIX_CASES_EMPTY")
        if category == "kvm" and len(result) < 16:
            if len(result) == 4:
                by_plan = {case["plan"]: case for case in result}
                result = [
                    by_plan[plan]
                    for plan in (
                        "kvm-4",
                        "kvm-4",
                        "kvm-1",
                        "kvm-1",
                        "kvm-2",
                        "kvm-2",
                        "kvm-4",
                        "kvm-4",
                        "kvm-1",
                        "kvm-1",
                        "kvm-2",
                        "kvm-2",
                        "kvm-3",
                        "kvm-3",
                        "kvm-3",
                        "kvm-3",
                    )
                ]
            else:
                result = (result * ((16 + len(result) - 1) // len(result)))[:16]
        return result
    # Mantener tres tandas físicas de seis. Un filtro de imagen conserva todas
    # las combinaciones requeridas y repite de forma balanceada hasta completar
    # las 16 posiciones de matriz.
    if len(result) < 16:
        result = (result * ((16 + len(result) - 1) // len(result)))[:16]
    elif len(result) > 16:
        raise ValueError("MATRIX_IMAGE_SELECTION_REQUIRES_TWO_IMAGES_PER_CYCLE")
    else:
        result = result[:16]
    controls = [dict(result[0], control=True), dict(result[-1], control=True)]
    if category == "services":
        # Controles explícitos para el contrato de acceso solo con contraseña.
        for case in controls:
            case["client_ssh_key"] = False
    return result + controls


def retry_after_delay(headers, attempt, *, now=None, jitter=None):
    """Respeta Retry-After y agrega jitter a reintentos idempotentes."""

    now = now or datetime.now(UTC)
    jitter = jitter or random.random
    fallback = 2**attempt
    raw = headers.get("Retry-After") if headers else None
    delay = fallback
    if raw:
        try:
            delay = max(0.0, float(raw))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(raw))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = max(0.0, (retry_at - now).total_seconds())
            except (TypeError, ValueError, OverflowError):
                delay = fallback
    return delay + jitter() * min(0.5, max(0.05, delay * 0.1))


class _NextcloudTokenParser(HTMLParser):
    """Read login CSRF tokens without retaining or logging page contents."""

    TOKEN_NAMES = {"requesttoken", "csrf-token", "csrf_token"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = 0
        self.input_names = set()
        self.request_token_attributes = set()
        self.meta_names = set()
        self.token = None

    def handle_starttag(self, tag, attributes):
        tag = tag.lower()
        attrs = {key.lower(): value for key, value in attributes if key}
        if tag == "form":
            self.forms += 1
        if tag == "input":
            name = (attrs.get("name") or "").lower()
            if name:
                self.input_names.add(name)
            if name in self.TOKEN_NAMES and attrs.get("value"):
                self.token = self.token or attrs["value"]
        if tag == "meta":
            name = (attrs.get("name") or "").lower()
            if name:
                self.meta_names.add(name)
            if name in self.TOKEN_NAMES and attrs.get("content"):
                self.token = self.token or attrs["content"]
        for name, value in attrs.items():
            if name in {"data-requesttoken", "data-csrf-token", "data-csrf_token"}:
                self.request_token_attributes.add(name)
                if value:
                    self.token = self.token or value


def nextcloud_request_token(source):
    parser = _NextcloudTokenParser()
    parser.feed(source)
    token = parser.token
    if not token:
        # Some Nextcloud releases render the token in OC's bootstrap object.
        for pattern in (
            r"[\"'](?:requesttoken|csrf[-_]token)[\"']\s*:\s*[\"']([^\"']+)",
            r"\b(?:requesttoken|csrf[-_]token)\s*=\s*[\"']([^\"']+)",
        ):
            match = re.search(pattern, source, re.IGNORECASE)
            if match:
                token = html.unescape(match.group(1))
                break
    shape = {
        "forms": parser.forms,
        "input_names": sorted(parser.input_names)[:30],
        "request_token_attributes": sorted(parser.request_token_attributes),
        "meta_names": sorted(parser.meta_names)[:30],
    }
    return token, shape


def ssh_failure_category(returncode, stderr):
    message = (stderr or "").lower()
    if returncode == 255:
        if "timed out" in message:
            return "transport_timeout"
        if "connection refused" in message:
            return "connection_refused"
        if "permission denied" in message:
            return "authentication_failed"
        if "host key verification failed" in message:
            return "host_key_rejected"
        return "transport_error"
    return "remote_command_error"


def allocate_matrix_names(category, planned, existing_names):
    """Create short, readable hostnames and skip names already in API history."""
    used = set(existing_names)
    next_number = {}
    names = []
    for case in planned:
        if category == "kvm":
            prefix = "kvm-prueba"
        elif category == "services":
            prefix = f"kvm-service-{case['product']}"
        elif category == "agents":
            prefix = f"kvm-agent-{case['product']}"
        else:
            raise ValueError("MATRIX_CATEGORY_INVALID")

        number = next_number.get(prefix, 1)
        while True:
            name = f"{prefix}-{number:03d}"
            if name not in used:
                break
            number += 1
        names.append(name)
        used.add(name)
        next_number[prefix] = number + 1
    return names


def matrix_runtime(env, production=False):
    """Bind a physical matrix to one stack and its non-overlapping VMID range."""

    if production:
        if (
            env.get("IACP_RUNTIME_STAGE") != "production"
            or env.get("IACP_VMID_MIN") != "30000"
            or env.get("IACP_VMID_MAX") != "39999"
        ):
            raise RuntimeError("PRODUCTION_STAGE_OR_VMID_RANGE_MISMATCH")
        return {
            "base_url": "https://iac.cyberfuel.com",
            "vmid_range": (30000, 39999),
            "api_container": "iacp-controlplane-prod-v1-api-1",
            "project": "iacp-controlplane-prod-v1",
        }
    if (
        env.get("IACP_RUNTIME_STAGE") != "develop"
        or env.get("IACP_VMID_MIN") != "40000"
        or env.get("IACP_VMID_MAX") != "49999"
    ):
        raise RuntimeError("DEVELOP_STAGE_OR_VMID_RANGE_MISMATCH")
    return {
        "base_url": "https://dev-iac.cyberfuel.com",
        "vmid_range": (40000, 49999),
        "api_container": "iacp-controlplane-dev-v1-api-1",
        "project": "iacp-controlplane-dev-v1",
    }


def matrix_netbox_tags(name):
    """Use the VM name as the human-visible NetBox test tag."""
    if not re.fullmatch(
        r"kvm-(?:prueba|service-[a-z0-9-]+|agent-[a-z0-9-]+)-[0-9]{3,}", name
    ):
        raise ValueError("MATRIX_NETBOX_TAG_INVALID")
    return [name]


class Matrix:
    def __init__(self, args):
        self.abort = threading.Event()
        self.api_lock = threading.Lock()
        self.last_request = 0.0
        self.args = args
        self.profile = args.network_profile
        self.service_timeout_seconds = int(
            getattr(args, "service_timeout_seconds", 14400)
        )
        if not 60 <= self.service_timeout_seconds <= 21600:
            raise ValueError("MATRIX_SERVICE_TIMEOUT_INVALID")
        self.env = read_env_file(args.env)
        if args.production:
            ipam_env = read_env_file(args.netbox_env)
            shared_ipam = {
                key: value
                for key, value in ipam_env.items()
                if key.startswith("IACP_NETBOX_") and value is not None
            }
            if not shared_ipam.get("IACP_NETBOX_TOKEN") or not shared_ipam.get(
                "IACP_NETBOX_URL"
            ):
                raise RuntimeError("DEVELOP_NETBOX_CREDENTIALS_REQUIRED")
            # El usuario autorizó usar el token de NetBox que ya reside en el
            # Control Plane de Dev. Solo vive en memoria del runner; prod.env
            # y la configuración del runtime no se modifican.
            self.env.update(shared_ipam)
        target = matrix_runtime(self.env, production=args.production)
        if args.production and self.profile != "prod_static":
            raise RuntimeError("PRODUCTION_MATRIX_REQUIRES_PROD_STATIC")
        if args.production and args.category != "services":
            raise RuntimeError("PRODUCTION_MATRIX_SCOPE_IS_SERVICES_ONLY")
        if args.production and (
            self.env.get("IACP_SERVICES_ENABLED") != "1"
            or self.env.get("IACP_SERVICE_EXECUTION_ENABLED") != "1"
            or self.env.get("IACP_PROD_STATIC_ENABLED") != "1"
            or self.env.get("IACP_PROD_STATIC_BRIDGE") != WAN_BRIDGE
        ):
            raise RuntimeError("PRODUCTION_SERVICE_OR_WAN_GATE_CLOSED")
        self.base = target["base_url"]
        self.vmid_range = target["vmid_range"]
        self.api_container = target["api_container"]
        self.project = target["project"]
        self.token = self.env["IACP_API_TOKEN"]
        assert 1 <= args.cycle <= required_cycles(args.category)
        self.static_bridge = WAN_BRIDGE if self.profile == "prod_static" else "vmbr0"
        self.folder = args.evidence.resolve()
        self.folder.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.key_directory = Path(tempfile.mkdtemp(prefix="iacp-service-cert-"))
        self.key_directory.chmod(0o700)
        self.secrets = []
        self.created_items = []
        self.created_lock = threading.Lock()
        self.run_started_at = datetime.now(UTC).isoformat()
        self.askpass = self.key_directory / "askpass"
        self.askpass.write_text("#!/bin/sh\nprintf '%s\\n' \"$IACP_MATRIX_PASSWORD\"\n")
        self.askpass.chmod(0o700)
        self.log = self.folder / "results.jsonl"
        original = socket.getaddrinfo
        socket.getaddrinfo = lambda host, *a, **kw: original(
            self.env["IACP_PROXMOX_MNG_HOST_IP"]
            if host == "mng01.cyberfuel.com"
            else host,
            *a,
            **kw,
        )
        self.proxmox = ProxmoxClient(
            self.env["IACP_PROXMOX_MNG_ENDPOINT"],
            self.env["IACP_PROXMOX_MNG_API_TOKEN"],
        )

    def verify_network_profile(self):
        plans_response = self.api("GET", "/v1/plans")
        plans = (
            plans_response
            if isinstance(plans_response, list)
            else plans_response.get("plans", [])
        )
        plans_by_id = {
            row.get("plan_id"): row for row in plans if isinstance(row, dict)
        }
        local_plans = {
            f"kvm-{number}": plan_by_id(f"kvm-{number}") for number in range(1, 5)
        }
        if any(
            not plans_by_id.get(plan_id)
            or any(
                plans_by_id[plan_id].get(field) != definition.get(field)
                for field in ("vcpus", "memory_mb", "disk_gb")
            )
            for plan_id, definition in local_plans.items()
            if definition
        ) or any(definition is None for definition in local_plans.values()):
            raise RuntimeError("MATRIX_PLAN_NOT_IN_CATALOG")
        if self.args.category == "services":
            app_response = self.api("GET", "/v1/applications")
            apps = (
                app_response
                if isinstance(app_response, list)
                else app_response.get("applications", [])
            )
            by_id = {
                row.get("application_id"): row
                for row in apps
                if isinstance(row, dict)
            }
            for application in ("nextcloud", "n8n"):
                row = by_id.get(application, {})
                if (
                    not row.get("enabled")
                    or row.get("image_id") != "ubuntu-2404"
                    or row.get("supported_image_ids") != ["ubuntu-2404"]
                ):
                    raise RuntimeError("MATRIX_SERVICE_CATALOG_MISMATCH")
        if self.profile == "prod_static":
            self.static_bridge = wan_nic_config(self.env)
            ipam_environment = {
                key: str(value)
                for key, value in self.env.items()
                if key.startswith("IACP_NETBOX_") and value is not None
            }
            try:
                with patch.dict(os.environ, ipam_environment):
                    netbox = NetBoxClient()
                    netbox_state = verify_netbox_state(netbox)
            except NetBoxError as error:
                raise RuntimeError("NETBOX_" + error.code) from None
            self.record("netbox_verified", **netbox_state)
            selected_images = self.args.images or (
                ["ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10"]
                if self.args.category == "kvm"
                else ["ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10"]
            )
            catalog_images = self.api("GET", "/v1/images")["images"]
            catalog_ids = {
                row.get("image_id")
                for row in catalog_images
                if isinstance(row, dict)
            }
            if not set(selected_images).issubset(catalog_ids):
                raise RuntimeError("MATRIX_IMAGE_NOT_IN_CATALOG")
            if "ubuntu-2404" in selected_images:
                self.verify_ubuntu_template(catalog_images)
            if self.args.exact_cases and self.args.category == "kvm":
                capacity = require_kvm_capacity(
                    self.proxmox.nodes(),
                    self.proxmox.storage_status("mng01", "DS_CYBERFUEL"),
                )
                self.record("kvm_capacity_verified", **capacity)
                self.verify_requested_templates(catalog_images, selected_images)
            selected_alma = [
                image for image in selected_images if image in EXPECTED_ALMALINUX_IMAGES
            ]
            golden_images = (
                require_expected_golden_images(
                    catalog_images,
                    self.proxmox.inventory(),
                    self.proxmox.vm_config,
                )
                if selected_alma
                else []
            )
            self.record("golden_images_verified", images=golden_images)
            profiles = self.api("GET", "/v1/network-profiles")["network_profiles"]
            wan = next(item for item in profiles if item["profile_id"] == "prod_static")
            assert wan["enabled"] is True, "WAN_PROFILE_DISABLED"
            assert wan["proxmox_bridge"] == WAN_BRIDGE, "WAN_BRIDGE_CONFIG_MISMATCH"
            self.record(
                "network_profile_verified",
                profile="prod_static",
                bridge=WAN_BRIDGE,
            )

    def verify_requested_templates(self, catalog_images, selected_images):
        selected = [
            image_id
            for image_id in selected_images
            if image_id in {"fedora-45", "debian-13"}
        ]
        inventory = self.proxmox.inventory()
        for image_id in selected:
            expected = image_by_id(image_id)
            published = next(
                (
                    row
                    for row in catalog_images
                    if isinstance(row, dict) and row.get("image_id") == image_id
                ),
                None,
            )
            if (
                not expected
                or not published
                or published.get("template_vm_id") != expected.get("template_vm_id")
                or published.get("source_node") != "mng01"
                or published.get("storage") != "DS_CYBERFUEL"
                or published.get("cloudinit_username") != "root"
            ):
                raise RuntimeError("KVM_GOLDEN_CATALOG_MISMATCH")
            vmid = int(expected["template_vm_id"])
            vm = next(
                (
                    row
                    for row in inventory
                    if isinstance(row, dict) and int(row.get("vmid", 0)) == vmid
                ),
                None,
            )
            if not vm or vm.get("node") != "mng01" or vm.get("status") != "stopped":
                raise RuntimeError("KVM_GOLDEN_INVENTORY_MISMATCH")
            config = self.proxmox.vm_config("mng01", vmid)
            root_disk = str(config.get("scsi0", ""))
            cloudinit_disk = str(config.get("scsi1", "")).lower()
            if (
                config.get("template") not in {1, "1", True}
                or "enabled=1" not in str(config.get("agent", ""))
                or not root_disk.startswith("DS_CYBERFUEL:")
                or "cloudinit" not in cloudinit_disk
            ):
                raise RuntimeError("KVM_GOLDEN_TEMPLATE_INVALID")
        self.record(
            "kvm_templates_verified",
            images=[{"image_id": image_id, "template_vm_id": image_by_id(image_id)["template_vm_id"]} for image_id in selected],
        )

    def verify_ubuntu_template(self, catalog_images):
        image = image_by_id("ubuntu-2404")
        local_plans = {
            f"kvm-{number}": plan_by_id(f"kvm-{number}") for number in range(1, 5)
        }
        published = next(
            (row for row in catalog_images if row.get("image_id") == "ubuntu-2404"),
            None,
        )
        if (
            not image
            or not published
            or image.get("template_vm_id") != 29000
            or image.get("source_node") != "mng01"
            or image.get("storage") != "DS_CYBERFUEL"
            or image.get("cloudinit_username") != "root"
            or published.get("template_vm_id") != 29000
            or published.get("source_node") != "mng01"
            or published.get("storage") != "DS_CYBERFUEL"
        ):
            raise RuntimeError("UBUNTU_GOLDEN_CATALOG_MISMATCH")
        inventory = self.proxmox.inventory()
        vm = next(
            (row for row in inventory if int(row.get("vmid", 0)) == 29000), None
        )
        if not vm or vm.get("node") != "mng01" or vm.get("status") != "stopped":
            raise RuntimeError("UBUNTU_GOLDEN_INVENTORY_MISMATCH")
        config = self.proxmox.vm_config("mng01", 29000)
        root_disk = next(
            (
                config.get(key, "")
                for key in ("scsi0", "virtio0", "sata0")
                if config.get(key)
            ),
            "",
        )
        root_size = re.search(r"(?:^|,)size=(\d+)G(?:,|$)", root_disk)
        cloudinit = any(
            str(config.get(key, "")).lower().startswith(
                image["storage"].lower() + ":"
            )
            and "cloudinit" in str(config.get(key, "")).lower()
            for key in ("ide2", "scsi1", "sata1", "virtio1")
        )
        if (
            config.get("template") not in {1, "1", True}
            or config.get("ostype") != "l26"
            or config.get("bios") != "ovmf"
            or config.get("machine") != "q35"
            or "enabled=1" not in config.get("agent", "")
            or not root_disk.startswith("DS_CYBERFUEL:")
            or not root_size
            or int(root_size.group(1)) < image["minimum_disk_gb"]
            or not cloudinit
        ):
            raise RuntimeError("UBUNTU_GOLDEN_HARDWARE_OR_CLOUDINIT_MISMATCH")
        node = next(
            (row for row in self.proxmox.nodes() if row.get("node") == "mng01"), None
        )
        storage = self.proxmox.storage_status("mng01", image["storage"])
        requested_cases = cases(
            "services",
            images=self.args.images,
            cycle=self.args.cycle,
            include_controls=not self.args.exact_cases,
        )
        batch = requested_cases[:6]
        required_memory = sum(
            plan_by_id(case["plan"])["memory_mb"] for case in batch
        )
        required_cores = sum(plan_by_id(case["plan"])["vcpus"] for case in batch)
        required_disk_gb = sum(
            plan_by_id(case["plan"])["disk_gb"] for case in batch
        )
        if (
            not node
            or node.get("status") != "online"
            or int(node.get("maxcpu", 0)) < required_cores
            or int(node.get("maxmem", 0)) - int(node.get("mem", 0))
            < (required_memory + 16_384) * 1024 * 1024
            or str(storage.get("active", "")).lower() not in {"1", "true"}
            or str(storage.get("enabled", "")).lower() not in {"1", "true"}
            or int(storage.get("avail", 0)) < required_disk_gb * 1_100_000_000
        ):
            raise RuntimeError("MNG_STATIC_BATCH_CAPACITY_INSUFFICIENT")
        for plan_id, definition in local_plans.items():
            if (
                definition["vcpus"] < image["minimum_vcpus"]
                or definition["memory_mb"] < image["minimum_memory_mb"]
                or definition["disk_gb"] < image["minimum_disk_gb"]
            ):
                raise RuntimeError("UBUNTU_PLAN_BELOW_IMAGE_MINIMUM")
        self.record(
            "ubuntu_template_verified",
            image_id="ubuntu-2404",
            template_vm_id=29000,
            node="mng01",
            storage="DS_CYBERFUEL",
            template=True,
            cloud_init=True,
            guest_agent=True,
            bios="ovmf",
            machine="q35",
            batch_vcpus=required_cores,
            batch_memory_mb=required_memory,
            batch_disk_gb=required_disk_gb,
            storage_avail_gb=round(int(storage["avail"]) / 1_000_000_000, 1),
        )

    def record(self, event, **data):
        with self.log.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "event": event,
                        **data,
                    }
                )
                + "\n"
            )
        try:
            print(json.dumps({"event": event, **data}), flush=True)
        except BrokenPipeError:
            # La evidencia persistente es autoritativa; perder SSH no debe detener
            # pruebas ni dejar recursos físicos sin la fase de limpieza.
            sys.stdout = open(os.devnull, "w")

    def check(self, item, check_id, evidence):
        self.record(
            "check",
            case=item["name"],
            check_id=check_id,
            status="PASS",
            evidence=evidence,
        )

    def api(self, method, path, body=None, key=None, raw=False):
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        }
        if key:
            headers["Idempotency-Key"] = key
        r = Request(
            self.base + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
        )
        for attempt in range(4):
            if self.abort.is_set() and method != "GET":
                raise RuntimeError("MATRIX_CONTAINMENT_ACTIVE")
            with self.api_lock:
                time.sleep(max(0, 0.25 - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
            try:
                with urlopen(r, timeout=90) as f:
                    if (
                        f.headers.get("X-IACP-Build-Revision")
                        != self.env["IACP_IMAGE_TAG"]
                    ):
                        raise RuntimeError("RUNTIME_REVISION_MISMATCH")
                    return f.read() if raw else json.load(f)
            except HTTPError as e:
                if e.code == 429 and (method == "GET" or key) and attempt < 3:
                    delay = retry_after_delay(e.headers, attempt)
                    self.record(
                        "api_retry",
                        method=method,
                        endpoint=path.split("?")[0],
                        status=429,
                        attempt=attempt + 1,
                        delay_seconds=round(delay, 2),
                        retry_after=e.headers.get("Retry-After"),
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"API_{e.code}_{method}_{path.split('/')[2]}"
                ) from None
            except (URLError, TimeoutError, ConnectionError):
                if (method == "GET" or key) and attempt < 3:
                    delay = retry_after_delay({}, attempt)
                    self.record(
                        "api_retry",
                        method=method,
                        endpoint=path.split("?")[0],
                        status="transport_error",
                        attempt=attempt + 1,
                        delay_seconds=round(delay, 2),
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"API_TRANSPORT_{method}_{path.split('/')[2]}"
                ) from None

    def parallel(self, function, items):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(function, item): item for item in items}
            results = {}
            failures = []
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                try:
                    results[item["name"]] = future.result()
                except Exception as error:
                    code = (
                        str(error)
                        if type(error) in {AssertionError, RuntimeError}
                        and re.fullmatch(r"[A-Z0-9_.:-]{1,100}", str(error))
                        else type(error).__name__
                    )
                    self.record("case_error", case=item["name"], error=code)
                    failures.append((item["name"], isinstance(error, AssertionError)))
        if failures:
            self.abort.set()
            if all(is_assertion for _, is_assertion in failures):
                raise AssertionError("MATRIX_PARALLEL_CASE_FAILURE")
            raise RuntimeError("MATRIX_PARALLEL_CASE_FAILURE")
        return [results[item["name"]] for item in items]

    def idle(self):
        state = self.api("GET", "/v1/observability")
        assert (
            state["max_concurrent"] == 6
            and state["active_operations"] == 0
            and state["queue_depth"] == 0
            and state["uncertain_operations"] == 0
        ), "RUNTIME_NOT_IDLE"
        assert len(state["workers"]) == 6 and all(
            w["status"] == "idle"
            and w["liveness"] == "healthy"
            and not w["current_operation_id"]
            for w in state["workers"]
        ), "SIX_WORKERS_REQUIRED"
        service_state = self.api("GET", "/v1/service-workers")
        require_four_idle_service_workers(service_state)
        query = """
from api.db import database
with database() as db:
    jobs=db.execute("SELECT COUNT(*) AS n FROM service_jobs WHERE status IN ('queued','running','uncertain')").fetchone()['n']
    locks=db.execute("SELECT COUNT(*) AS n FROM pg_locks WHERE locktype='advisory'").fetchone()['n']
print(f"{jobs} {locks}")
"""
        checked = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.api_container,
                "/app/.venv/bin/python",
                "-",
            ],
            input=query,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if checked.returncode:
            raise RuntimeError("SERVICE_QUEUE_PREFLIGHT_FAILED")
        try:
            active_jobs, advisory_locks = map(int, checked.stdout.split())
        except (ValueError, TypeError):
            raise RuntimeError("SERVICE_QUEUE_PREFLIGHT_FAILED") from None
        if active_jobs or advisory_locks:
            raise RuntimeError("SERVICE_QUEUE_NOT_IDLE")
        self.record(
            "quiescence",
            infrastructure_workers=len(state["workers"]),
            service_workers=len(service_state["workers"]),
            active_service_jobs=active_jobs,
            advisory_locks=advisory_locks,
        )
        return state

    def wait_operation(self, job):
        for _ in range(450):
            r = self.api("GET", "/v1/operations/" + job["operation_id"])
            if r["status"] in {"completed", "succeeded"}:
                return r
            if r["status"] in {"failed", "uncertain"}:
                raise RuntimeError("OPERATION_" + r["status"].upper())
            time.sleep(2)
        raise RuntimeError("OPERATION_TIMEOUT")

    def wait_for_ssh_after_power_on(self, item, timeout=180):
        """Espera a que el invitado acepte SSH y ambas credenciales funcionen."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((item["address"], 22), timeout=5):
                    pass
            except OSError:
                time.sleep(2)
                continue
            try:
                hostname = self.ssh(item, ["hostname"], timeout=20).strip()
                assert hostname == item["name"], "SSH_HOSTNAME_MISMATCH"
                hostname = self.ssh(
                    item, ["hostname"], password=item["password"], timeout=20
                ).strip()
                assert hostname == item["name"], "SSH_PASSWORD_HOSTNAME_MISMATCH"
                return
            except RuntimeError as error:
                if str(error).startswith(
                    ("SSH_TRANSPORT_TIMEOUT_", "SSH_CONNECTION_REFUSED_")
                ):
                    time.sleep(2)
                    continue
                raise
        raise RuntimeError("SSH_READINESS_TIMEOUT")

    def qga(self, vmid, argv, *, allowed_exitcodes=(0,)):
        result = self.proxmox.guest_agent_exec("mng01", vmid, argv)
        for _ in range(90):
            state = self.proxmox.guest_agent_exec_status("mng01", vmid, result["pid"])
            if state.get("exited"):
                assert state.get("exitcode") in allowed_exitcodes, "QGA_COMMAND_FAILED"
                return state.get("out-data", "")
            time.sleep(1)
        raise RuntimeError("QGA_TIMEOUT")

    def binding(self, item):
        vmid = item["vmid"]
        config = self.proxmox.vm_config("mng01", vmid)
        nic = dict(
            field.split("=", 1)
            for field in config["net0"].split(",")
            if "=" in field
        )
        assert config["name"] == item["name"]
        if self.profile == "prod_static":
            assert nic.get("bridge") == self.static_bridge
            assert "tag" not in nic
            assert config["ipconfig0"] in [
                f"ip={item['address']}/24,gw=190.0.231.1",
                f"gw=190.0.231.1,ip={item['address']}/24",
            ]
        else:
            assert nic.get("bridge") == "vmbr0"
            assert "tag=" not in config["net0"] and config["ipconfig0"] == "ip=dhcp"
        known = self.folder / ("known-" + str(vmid))
        if item["image"] == "fedora-45":
            mac = config["net0"].split(",")[0].split("=", 1)[1].lower()
            addresses = [
                str(entry.get("ip-address", ""))
                for interface in self.proxmox.guest_agent_interfaces("mng01", vmid)
                if str(interface.get("hardware-address", "")).lower() == mac
                for entry in interface.get("ip-addresses", [])
                if entry.get("ip-address-type") == "ipv4"
            ]
            assert addresses == [item["address"]], "FEDORA_GUEST_AGENT_ADDRESS_MISMATCH"
            scanned = subprocess.run(
                ["ssh-keyscan", "-T", "10", "-t", "ed25519", item["address"]],
                capture_output=True,
                text=True,
                timeout=20,
            )
            known_hosts = [
                line
                for line in scanned.stdout.splitlines()
                if line and not line.startswith("#") and " ssh-ed25519 " in line
            ]
            assert scanned.returncode == 0 and known_hosts, "FEDORA_SSH_KEYSCAN_FAILED"
            known.write_text("\n".join(known_hosts) + "\n")
            item["known"] = str(known)
            item["host_key_source"] = "ssh-keyscan; autenticación posterior con clave efímera"
            return
        if item["image"] in {"almalinux-9", "almalinux-10"}:
            mac = config["net0"].split(",")[0].split("=", 1)[1].lower()
            observed = json.loads(self.qga(vmid, [
                "/usr/local/libexec/iacp-guest-bootstrap", item["instance_id"], mac,
                "static" if self.profile == "prod_static" else "dhcp",
                item["address"] + "/24" if self.profile == "prod_static" else "",
                "190.0.231.1" if self.profile == "prod_static" else "",
            ]))
            assert observed.get("bootstrap_protocol") == 1
            assert observed.get("instance_id") == item["instance_id"] and observed.get("mac_address") == mac
            assert not observed.get("ipv4") or observed["ipv4"] == item["address"]
            hostkey = observed.get("host_key", "")
        else:
            hostkey = " ".join(self.qga(vmid, ["/bin/cat", "/etc/ssh/ssh_host_ed25519_key.pub"]).split()[:2])
        assert re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", hostkey), "SSH_HOST_KEY_INVALID"
        known.write_text(item["address"] + " " + hostkey + "\n")
        item["known"] = str(known)

    def hardware(self, item, instance):
        image = image_by_id(item["image"])
        plan = plan_by_id(item["plan"])
        assert image, "SERVICE_IMAGE_NOT_IN_CATALOG"
        assert plan, "SERVICE_PLAN_NOT_IN_CATALOG"
        assert instance.get("image_id") == item["image"]
        assert instance.get("plan_id") == item["plan"]
        assert instance.get("node") == "mng01"
        assert instance.get("status") == "ready"
        config = self.proxmox.vm_config("mng01", item["vmid"])
        template = self.proxmox.vm_config("mng01", image["template_vm_id"])
        assert config.get("name") == item["name"] and not config.get("template")
        assert int(config.get("cores", 0)) == plan["vcpus"], "VM_CPU_MISMATCH"
        assert int(config.get("memory", 0)) == plan["memory_mb"], "VM_MEMORY_MISMATCH"
        assert config.get("bios") == template.get("bios"), "VM_FIRMWARE_MISMATCH"
        assert config.get("machine") == template.get("machine"), "VM_MACHINE_MISMATCH"
        disk = next(
            (
                config.get(key, "")
                for key in ("scsi0", "virtio0", "sata0")
                if config.get(key)
            ),
            "",
        )
        size = re.search(r"(?:^|,)size=(\d+)G(?:,|$)", disk)
        assert size and int(size.group(1)) == plan["disk_gb"], "VM_DISK_SIZE_MISMATCH"
        assert disk.startswith(image["storage"] + ":"), "VM_STORAGE_MISMATCH"
        assert "enabled=1" in config.get("agent", ""), "VM_GUEST_AGENT_DISABLED"
        events = self.api(
            "GET",
            "/v1/events?operation_id=" + item["operation_id"] + "&sort=asc&limit=500",
        ).get("events", [])
        assert any(
            row.get("kind") in {"golden.observed", "image.validated"}
            and row.get("status") == "ok"
            and row.get("detail") == item["image"]
            for row in events
        ), "GOLDEN_IMAGE_CLONE_NOT_PROVEN"
        self.check(
            item,
            "A03",
            f"evento golden.observed de {item['image']}/{image['template_vm_id']}; CPU, RAM, disco, firmware, red, QGA y DS_CYBERFUEL verificados",
        )

    def reject_ssh_password(self, item, password):
        args = [
            "ssh",
            "-o",
            "BatchMode=no",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + item["known"],
            "-o",
            "ConnectTimeout=15",
            "root@" + item["address"],
            "true",
        ]
        environment = os.environ.copy()
        environment.update(
            SSH_ASKPASS=str(self.askpass),
            SSH_ASKPASS_REQUIRE="force",
            DISPLAY=":iacp",
            IACP_MATRIX_PASSWORD=password,
        )
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
            start_new_session=True,
        )
        return result.returncode != 0

    def ssh(self, item, argv, input=None, password=None, timeout=240):
        if self.abort.is_set():
            raise RuntimeError("MATRIX_CONTAINMENT_ACTIVE")
        if not item.get("client_ssh_key", True) and password is None:
            password = item["password"]
        args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + item["known"],
            "-o",
            "ConnectTimeout=15",
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            str(item["client_key_path"]),
            "root@" + item["address"],
            shlex.join(argv),
        ]
        environment = os.environ.copy()
        if password is not None:
            args[args.index("BatchMode=yes")] = "BatchMode=no"
            args[1:1] = [
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
            environment.update(
                SSH_ASKPASS=str(self.askpass),
                SSH_ASKPASS_REQUIRE="force",
                DISPLAY=":iacp",
                IACP_MATRIX_PASSWORD=password,
            )
        try:
            result = subprocess.run(
                args,
                input=input,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=environment,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            program = (
                str(argv[0])
                if argv and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", str(argv[0]))
                else "unknown"
            )
            self.record(
                "ssh_failure",
                case=item.get("name", "unknown"),
                program=program,
                category="transport_timeout",
                exit_code="timeout",
            )
            raise RuntimeError(
                f"SSH_TRANSPORT_TIMEOUT_{program.upper()}_TIMEOUT"
            ) from None
        if result.returncode:
            program = str(argv[0]) if argv and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", str(argv[0])) else "unknown"
            category = ssh_failure_category(result.returncode, result.stderr)
            self.record(
                "ssh_failure",
                case=item.get("name", "unknown"),
                program=program,
                category=category,
                exit_code=result.returncode,
            )
            raise RuntimeError(
                f"SSH_{category.upper()}_{program.upper()}_{abs(result.returncode)}"
            )
        return result.stdout

    def create(self, index, case):
        name = self.matrix_names[index - 1]
        password = secrets.token_urlsafe(32)
        application_user = None
        application_email = None
        application_password = None
        key_file = self.key_directory / f"client-{index:02d}"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_file)],
            check=True,
        )
        self.secrets.append(key_file.read_text())
        public_key = key_file.with_suffix(".pub").read_text().strip()
        self.secrets.append(password)
        body = {
            "name": name,
            "plan_id": case["plan"],
            "image_id": case["image"],
            "environment": "lab",
            "network_profile": self.profile,
            "access": {
                "ssh_public_keys": [public_key] if case.get("client_ssh_key", True) else [],
                "root_password": password,
            },
            # NetBox should identify a test allocation by its readable VM
            # hostname; the run identifier remains in the evidence directory.
            "tags": matrix_netbox_tags(name),
            "start_on_create": True,
        }
        if case["product"] == "kvm":
            endpoint = "/v1/instances"
        else:
            config = (
                {"access_mode": "ip"}
                if case["product"] in {"nextcloud", "n8n"}
                else {"agent_mode": "local"}
            )
            application_password = secrets.token_urlsafe(32) + "Aa9!"
            self.secrets.append(application_password)
            application_secrets = {}
            if case["product"] == "nextcloud":
                application_user = f"cliente{index:03d}"
                config["admin_user"] = application_user
                application_secrets["admin_password"] = application_password
            elif case["product"] == "n8n":
                application_email = f"e2e{self.args.run}{index:03d}@example.invalid"
                config.update(
                    {
                        "owner_email": application_email,
                        "owner_first_name": "Prueba",
                        "owner_last_name": "Servicio",
                    }
                )
                application_secrets["owner_password"] = application_password
            endpoint = "/v1/service-instances"
            body = {
                    "application_id": case["product"],
                    "instance": body,
                    "configuration": config,
                    "secrets": application_secrets,
                }
        result = self.api("POST", endpoint, body, name)
        item = dict(
            case,
            **result,
            name=name,
            password=password,
            application_user=application_user,
            application_email=application_email,
            application_password=application_password,
            client_key_path=str(key_file),
        )
        with self.created_lock:
            self.created_items.append(item)
        replay = self.api("POST", endpoint, body, name)
        assert all(result.get(key) == replay.get(key) for key in ("instance_id", "operation_id")), "IDEMPOTENCY_REPLAY_MISMATCH"
        if case["product"] != "kvm":
            assert result.get("service_id") == replay.get("service_id"), "IDEMPOTENCY_SERVICE_REPLAY_MISMATCH"
        self.check(item, "A01", "GET /v1/images, /v1/applications y /v1/plans; imagen y plan habilitados")
        self.check(item, "A02", "POST JSON admitido; replay idempotente devuelve la misma instancia y operación")
        return item

    def web(
        self, item, path, method="GET", data=None, extra_headers=None, basic_auth=True
    ):
        context = ssl.create_default_context(cafile=str(item["ca"]))
        credentials = item["credentials"]
        headers = {}
        if basic_auth:
            user = credentials.get("admin_user")
            password = credentials.get("admin_password")
            if not user or not password:
                raise RuntimeError("WEB_CREDENTIALS_UNAVAILABLE")
            authorization = base64.b64encode(
                (user + ":" + password).encode()
            ).decode()
            headers["Authorization"] = "Basic " + authorization
        if data is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        r = Request(
            "https://" + item["address"] + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(r, context=context, timeout=60) as f:
                return f.status, f.read(), f.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def nextcloud_gui_login(self, item, *, invalid_password=False):
        """Envía el formulario real de login y verifica una sesión autenticada."""

        context = ssl.create_default_context(cafile=str(item["ca"]))
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(
            HTTPSHandler(context=context), HTTPCookieProcessor(cookies)
        )
        root = "https://" + item["address"]
        login_url = root + "/login"

        def read_page(request):
            try:
                with opener.open(request, timeout=60) as response:
                    return response.status, response.read(), response.geturl()
            except HTTPError as error:
                return error.code, error.read(), error.geturl()

        status, page, _ = read_page(Request(login_url))
        assert status == 200, "NEXTCLOUD_LOGIN_PAGE_UNAVAILABLE"
        source = page.decode("utf-8", errors="replace")
        token, page_shape = nextcloud_request_token(source)
        if not token:
            self.record(
                "nextcloud_login_form_diagnostic",
                case=item["name"],
                **page_shape,
            )
            raise AssertionError("NEXTCLOUD_GUI_CSRF_TOKEN_MISSING")
        credentials = item["credentials"]
        password = credentials["admin_password"] + ("X" if invalid_password else "")
        payload = urlencode(
            {
                "user": credentials["admin_user"],
                "password": password,
                "requesttoken": token,
            }
        ).encode()
        request = Request(
            login_url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": login_url,
                "Origin": root,
            },
        )
        login_status, _, landing = read_page(request)
        path = urlsplit(landing).path.lower()
        if invalid_password:
            assert login_status in {200, 302, 303, 401, 403} and "login" in path, (
                "NEXTCLOUD_GUI_INVALID_PASSWORD_ACCEPTED"
            )
            self.check(item, "C06", "formulario GUI Nextcloud rechaza contraseña incorrecta")
            return
        assert login_status == 200 and "login" not in path, "NEXTCLOUD_GUI_LOGIN_FAILED"
        dashboard_status, body, dashboard_url = read_page(
            Request(root + "/index.php/apps/files/")
        )
        assert (
            dashboard_status == 200
            and "login" not in urlsplit(dashboard_url).path.lower()
            and b"files" in body.lower()
        ), (
            "NEXTCLOUD_GUI_SESSION_NOT_AUTHENTICATED"
        )
        self.check(item, "C04", "login GUI Nextcloud crea sesión autenticada en Archivos")

    def wait_application_ready(self, item, timeout=600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if item["product"] == "nextcloud":
                    status, body, _ = self.web(item, "/status.php", basic_auth=False)
                    ready = status == 200 and json.loads(body).get("installed") is True
                else:
                    status, body, _ = self.web(
                        item, "/healthz/readiness", basic_auth=False
                    )
                    ready = status == 200 and json.loads(body).get("status") == "ok"
                if ready:
                    return
            except (OSError, ValueError, TimeoutError):
                pass
            time.sleep(2)
        raise RuntimeError("SERVICE_READINESS_TIMEOUT")

    def n8n_owner_auth(self, item, setup_owner, check_invalid=True):
        credentials = item.get("credentials", {})
        email = credentials.get("owner_email") or f"prueba-n8n-{item['vmid']}@example.invalid"
        if setup_owner and not credentials.get("owner_password"):
            item["n8n_owner_password"] = secrets.token_urlsafe(32) + "Aa9!"
            self.secrets.append(item["n8n_owner_password"])
        password = credentials.get("owner_password") or item.get("n8n_owner_password")
        assert password, "N8N_OWNER_PASSWORD_NOT_PRESERVED"
        if setup_owner:
            status, body, _ = self.web(
                item,
                "/rest/owner/setup",
                "POST",
                json.dumps({
                    "email": email,
                    "firstName": "Prueba",
                    "lastName": "Servicio",
                    "password": password,
                }).encode(),
                basic_auth=False,
            )
            result = json.loads(body)
            assert status == 200 and (result.get("data") or result).get("email") == email, "N8N_OWNER_SETUP_FAILED"
        status, body, response_headers = self.web(
            item,
            "/rest/login",
            "POST",
            json.dumps({"emailOrLdapLoginId": email, "password": password}).encode(),
            basic_auth=False,
        )
        result = json.loads(body)
        assert status == 200 and (result.get("data") or result).get("email") == email, "N8N_OWNER_LOGIN_FAILED"
        cookies = response_headers.get_all("Set-Cookie", [])
        cookie = "; ".join(value.split(";", 1)[0] for value in cookies)
        assert cookie, "N8N_OWNER_SESSION_MISSING"
        if check_invalid:
            status, _, _ = self.web(
                item,
                "/rest/login",
                "POST",
                json.dumps({"emailOrLdapLoginId": email, "password": password + "X"}).encode(),
                basic_auth=False,
            )
            assert status in {401, 403}, "N8N_INVALID_PASSWORD_ACCEPTED"
            self.check(item, "C06", "n8n rechaza credencial administrativa incorrecta")
        status, body, _ = self.web(
            item,
            "/rest/workflows?skip=0&take=10",
            extra_headers={"Cookie": cookie},
            basic_auth=False,
        )
        assert status == 200 and any(
            workflow.get("id") == "iacpValidation"
            for workflow in json.loads(body).get("data", [])
        ), "N8N_OWNER_WORKFLOW_ACCESS_FAILED"
        self.check(item, "C04", "login n8n y sesión administrativa autenticada")

    def containers_running(self, item):
        expected = (
            {"app", "postgres", "redis", "cron", "caddy"}
            if item["product"] == "nextcloud"
            else {"app", "postgres", "caddy"}
        )
        root = "/opt/iacp-services/" + item["service_id"]
        output = self.ssh(
            item,
            [
                "docker",
                "compose",
                "--project-directory",
                root,
                "ps",
                "--services",
                "--status",
                "running",
            ],
        )
        assert expected.issubset(set(output.splitlines())), "SERVICE_CONTAINERS_NOT_RUNNING"
        self.check(item, "C02", "todos los contenedores esperados están ejecutándose")

    def application(self, item, after_reboot=False):
        product = item["product"]
        root = "/opt/iacp-services/" + item["service_id"]
        base = ["docker", "compose", "--project-directory", root]
        self.wait_application_ready(item)
        self.containers_running(item)
        if product == "nextcloud":
            user = item["credentials"]["admin_user"]
            folder = f"/remote.php/dav/files/{user}/iacp-certification"
            path = folder + "/iacp-persistence.txt"
            if not after_reboot:
                page_status, _, _ = self.web(item, "/login", basic_auth=False)
                assert page_status == 200, "NEXTCLOUD_GUI_UNAVAILABLE"
                self.check(item, "C03", "GUI Nextcloud responde por HTTPS con CA y SAN IP válidos")
                self.nextcloud_gui_login(item)
                self.nextcloud_gui_login(item, invalid_password=True)
                created, _, _ = self.web(item, folder, "MKCOL")
                assert created == 201, "NEXTCLOUD_FOLDER_CREATE_FAILED"
            if not after_reboot:
                status, _, _ = self.web(item, path, "PUT", b"iacp-persistence")
                assert status in {201, 204}, "NEXTCLOUD_UPLOAD_FAILED"
            status, downloaded, _ = self.web(item, path)
            checksum = hashlib.sha256(downloaded).hexdigest()
            assert status == 200 and checksum == hashlib.sha256(b"iacp-persistence").hexdigest(), "NEXTCLOUD_FILE_CHECKSUM_MISMATCH"
            item["nextcloud_checksum"] = checksum
            if not after_reboot:
                self.check(item, "C04", "WebDAV autentica al usuario administrador")
                self.check(item, "C05", "archivo sintético descargado con checksum SHA-256 verificado")
                self.check(item, "D01", "archivo persistente escrito y comprobado")
            elif item.get("nextcloud_checksum") == checksum:
                self.check(item, "D05", "archivo y checksum permanecen tras reinicio")
        elif product == "n8n":
            readiness, body, _ = self.web(
                item, "/healthz/readiness", basic_auth=False
            )
            assert readiness == 200 and json.loads(body).get("status") == "ok", "N8N_NOT_READY"
            assert self.web(item, "/", basic_auth=False)[0] == 200
            if not after_reboot:
                self.check(item, "C03", "GUI n8n responde por HTTPS con validación de CA y SAN IP")
            if not after_reboot:
                workflow = {
                    "id": "iacpValidation",
                    "name": "IACP validacion",
                    "nodes": [
                        {
                            "parameters": {},
                            "id": "start",
                            "name": "Start",
                            "type": "n8n-nodes-base.manualTrigger",
                            "typeVersion": 1,
                            "position": [0, 0],
                        },
                        {
                            "parameters": {
                                "jsCode": "return [{json:{iacpValidation:true}}];"
                            },
                            "id": "code",
                            "name": "Code",
                            "type": "n8n-nodes-base.code",
                            "typeVersion": 2,
                            "position": [200, 0],
                        },
                    ],
                    "connections": {
                        "Start": {
                            "main": [[{"node": "Code", "type": "main", "index": 0}]]
                        }
                    },
                    "settings": {"executionOrder": "v1"},
                    "active": False,
                }
                self.ssh(
                    item,
                    base
                    + [
                        "exec",
                        "-T",
                        "app",
                        "sh",
                        "-c",
                        "cat > /home/node/.n8n/iacp-workflow.json",
                    ],
                    input=json.dumps(workflow),
                )
                self.ssh(
                    item,
                    base
                    + [
                        "exec",
                        "-T",
                        "app",
                        "n8n",
                        "import:workflow",
                        "--input=/home/node/.n8n/iacp-workflow.json",
                    ],
                )
            output = self.ssh(
                item,
                base + ["exec", "-T", "-e", "N8N_RUNNERS_BROKER_PORT=5681", "app", "n8n", "execute", "--id=iacpValidation"],
            )
            assert "iacpValidation" in output and "true" in output, (
                "N8N_WORKFLOW_FAILED"
            )
            if not after_reboot:
                self.check(item, "C05", "workflow sintético n8n ejecutado y resultado confirmado")
                self.check(item, "D01", "workflow de prueba persistido en el volumen de datos")
            self.n8n_owner_auth(
                item,
                setup_owner=bool(item["credentials"].get("owner_setup_required"))
                and not after_reboot,
                check_invalid=not after_reboot,
            )
        elif product == "hermes":
            self.ssh(item, base + ["exec", "-T", "app", "hermes", "config", "check"])
        else:
            self.ssh(
                item, base + ["exec", "-T", "app", "node", "openclaw.mjs", "health"]
            )
        states = (
            json.loads(self.ssh(item, base + ["ps", "--format", "json"]))
            if product in {"hermes", "openclaw"}
            else None
        )
        if states is not None:
            states = states if isinstance(states, list) else [states]
            assert states and all(s["State"] == "running" for s in states)

    def verify(self, item):
        instance = self.api("GET", "/v1/instances/" + item["instance_id"])
        item["vmid"] = instance["vmid"]
        assert self.vmid_range[0] <= item["vmid"] <= self.vmid_range[1]
        self.hardware(item, instance)
        self.check(item, "A06", "la API publica estado READY, nodo mng01 e instancia encendida")
        if item["image"] == "fedora-45":
            self.check(item, "A04", "la clave configurada por Cloud-Init autentica por SSH; SELinux restringe cloud-init status vía Guest Agent")
        else:
            cloud = json.loads(
                self.qga(
                    item["vmid"],
                    ["/usr/bin/cloud-init", "status", "--long", "--format", "json"],
                    allowed_exitcodes=(0, 2),
                )
            )
            assert cloud.get("status") == "done" and cloud.get("errors") == [], "CLOUD_INIT_NOT_COMPLETE"
            self.check(item, "A04", "Cloud-Init informa done sin errores mediante Guest Agent")
        allocation = instance["network_allocation"]
        if self.profile == "prod_static":
            assert (
                allocation["status"] == "allocated"
                and allocation["gateway"] == "190.0.231.1"
            )
            item["address"] = allocation["address"].split("/")[0]
            assert item["address"].startswith("190.0.231.")
        else:
            assert allocation is None
            config = self.proxmox.vm_config("mng01", item["vmid"])
            mac = config["net0"].split(",")[0].split("=")[1].lower()
            addresses = [
                entry["ip-address"]
                for interface in self.proxmox.guest_agent_interfaces(
                    "mng01", item["vmid"]
                )
                if interface.get("hardware-address", "").lower() == mac
                for entry in interface.get("ip-addresses", [])
                if entry.get("ip-address-type") == "ipv4"
            ]
            assert len(addresses) == 1, "DHCP_ADDRESS_AMBIGUOUS"
            item["address"] = addresses[0]
        self.binding(item)
        self.check(item, "A05", "Guest Agent, IPAM, NIC, gateway e identidad SSH coinciden")
        assert self.ssh(item, ["hostname"]).strip() == item["name"]
        self.check(item, "B01", "autenticación SSH Ed25519 válida")
        assert (
            self.ssh(item, ["hostname"], password=item["password"]).strip()
            == item["name"]
        )
        self.check(item, "B02", "autenticación SSH con la credencial de root configurada")
        assert self.reject_ssh_password(item, item["password"] + "X"), "SSH_INVALID_PASSWORD_ACCEPTED"
        self.check(item, "B03", "SSH rechaza una credencial incorrecta")
        self.ssh(
            item,
            [
                "sh",
                "-c",
                "set -e; ip route show default | grep -q default; "
                "for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do "
                "getent ahostsv4 registry-1.docker.io >/dev/null && exit 0; "
                "sleep 2; done; exit 1",
            ],
        )
        self.check(item, "A05", "ruta por defecto y resolución DNS de registry OCI disponibles")
        self.ssh(
            item, ["sh", "-c", "printf iacp-persistence > /root/iacp-validation.txt"]
        )
        self.check(item, "D01", "marcador sintético de persistencia escrito en el sistema")
        if item["product"] != "kvm":
            item["credentials"] = self.api(
                "POST", "/v1/service-instances/" + item["service_id"] + "/credentials"
            )
            if item["credentials"].get("admin_password"):
                self.secrets.append(item["credentials"]["admin_password"])
            if item["product"] == "nextcloud":
                assert item["credentials"].get("admin_user") == item["application_user"]
                assert item["credentials"].get("admin_password") == item["application_password"]
            elif item["product"] == "n8n":
                assert item["credentials"].get("owner_email") == item["application_email"]
                assert item["credentials"].get("owner_password") == item["application_password"]
                self.secrets.append(item["credentials"]["owner_password"])
            if item["product"] in {"nextcloud", "n8n"}:
                ca = self.folder / ("ca-" + str(item["vmid"]) + ".crt")
                ca.write_bytes(
                    self.api(
                        "GET",
                        "/v1/service-instances/" + item["service_id"] + "/tls-ca",
                        raw=True,
                    )
                )
                item["ca"] = ca
            self.application(item)
            delivery = self.api(
                "POST",
                "/v1/service-instances/" + item["service_id"] + "/deliver",
                key="delivery-" + item["name"],
            )
            self.wait_service(delivery["job_id"])
            self.check(item, "C01", "bootstrap del servicio completado por el worker y entrega canónica registrada")
            root = "/opt/iacp-services/" + item["service_id"]
            self.ssh(item, ["docker", "compose", "--project-directory", root, "restart"])
            self.application(item, True)
            self.check(item, "D02", "contenedores reiniciados y servicio volvió a responder")
        self.wait_operation(
            self.api(
                "POST",
                "/v1/instances/" + item["instance_id"] + "/power/reboot",
                key="reboot-" + item["name"],
            )
        )
        self.check(item, "D03", "reinicio de la KVM completado mediante API")
        for attempt in range(60):
            try:
                assert (
                    self.ssh(item, ["cat", "/root/iacp-validation.txt"])
                    == "iacp-persistence"
                )
                assert self.ssh(item, ["hostname"], password=item["password"]).strip() == item["name"]
                self.check(item, "B04", "clave SSH y contraseña siguen válidas tras reinicio")
                if item["product"] != "kvm":
                    self.application(item, True)
                break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(3)
        shutdown = self.api(
            "POST",
            "/v1/instances/" + item["instance_id"] + "/power/shutdown",
            key="lifecycle-shutdown-" + item["name"],
        )
        self.wait_operation(shutdown)
        stopped = self.api("GET", "/v1/instances/" + item["instance_id"])
        assert stopped["power_state"] == "stopped" and stopped["status"] == "stopped"
        physical = self.proxmox.power_state("mng01", item["vmid"], item["name"])
        assert physical["power_state"] == "stopped"
        started = self.api(
            "POST",
            "/v1/instances/" + item["instance_id"] + "/power/start",
            key="lifecycle-start-" + item["name"],
        )
        self.wait_operation(started)
        running = self.api("GET", "/v1/instances/" + item["instance_id"])
        physical = self.proxmox.power_state("mng01", item["vmid"], item["name"])
        assert running["status"] == "ready" and running["power_state"] == "running"
        assert physical["power_state"] == "running"
        self.wait_for_ssh_after_power_on(item)
        if item["product"] != "kvm":
            self.application(item, True)
        self.check(item, "D04", "apagado y encendido ordenados mediante API")
        self.check(item, "D05", "accesos y datos del servicio continúan después de apagado/encendido")
        self.check(item, "D06", "estado API y estado físico Proxmox coinciden")
        self.record(
            "verified",
            name=item["name"],
            vmid=item["vmid"],
            address=item["address"],
            product=item["product"],
            image=item["image"],
            plan=item["plan"],
            control=item["control"],
        )

    def wait_for_service_worker_release(self, jobid, worker_id):
        # A service worker records the terminal job state immediately before
        # publishing its idle heartbeat. Poll that short handoff window instead
        # of treating the expected heartbeat race as a failed matrix case.
        for _ in range(30):
            workers = self.api("GET", "/v1/service-workers").get("workers", [])
            owner = next(
                (worker for worker in workers if worker.get("worker_id") == worker_id),
                None,
            )
            if owner is None:
                raise RuntimeError("SERVICE_PREVIOUS_EXECUTION_NOT_STOPPED")
            if owner.get("status") not in {"idle", "running"}:
                raise RuntimeError("SERVICE_PREVIOUS_EXECUTION_NOT_STOPPED")
            if owner.get("job_id") != jobid:
                return
            time.sleep(1)
        raise RuntimeError("SERVICE_PREVIOUS_EXECUTION_NOT_STOPPED")

    def wait_service(self, jobid):
        retry_attempted = False
        retryable_errors = {
            "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_SSH_CONNECTIVITY",
            "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_SERVICE_EGRESS_PREFLIGHT",
            "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_APPLICATION_IMAGE_PULL",
            "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_FAILED_APPLICATION_START",
            "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_UNREACHABLE_APPLICATION_START",
        }
        timeout_seconds = getattr(self, "service_timeout_seconds", 14400)
        for _ in range(max(1, (timeout_seconds + 1) // 2)):
            job = self.api("GET", "/v1/service-jobs/" + jobid)
            if job["status"] == "succeeded":
                return
            if (
                not retry_attempted
                and job["status"] in {"failed", "uncertain"}
                and job.get("error") in retryable_errors
            ):
                worker_id = job.get("worker_id")
                if not worker_id:
                    raise RuntimeError("SERVICE_PREVIOUS_EXECUTION_NOT_STOPPED")
                self.wait_for_service_worker_release(jobid, worker_id)
                response = self.api(
                    "POST",
                    "/v1/service-jobs/" + jobid + "/retry",
                    {"previous_execution_stopped": True},
                    key="matrix-service-retry-" + jobid,
                )
                if response.get("status") != "queued":
                    raise RuntimeError("SERVICE_RETRY_NOT_QUEUED")
                self.record(
                    "service_retry",
                    job_id=jobid,
                    worker_id=worker_id,
                    previous_status=job["status"],
                    error_code=job["error"],
                    attempt=2,
                )
                retry_attempted = True
                continue
            if job["status"] in {"blocked", "failed", "uncertain", "cancelled"}:
                raise RuntimeError(
                    "SERVICE_" + job["status"].upper() + "_" + str(job.get("error", ""))
                )
            time.sleep(2)
        raise RuntimeError("SERVICE_TIMEOUT")

    def settle_service_batch(self, items, timeout=900):
        """Wait for the whole service batch to stop before recovery or deletion."""

        jobs = {item["job_id"]: item for item in items if item.get("job_id")}
        items_by_job = dict(jobs)
        deadline = time.monotonic() + timeout
        terminal = {"succeeded", "failed", "uncertain", "blocked", "cancelled"}
        states = {}
        while jobs:
            for job_id, item in list(jobs.items()):
                job = self.api("GET", "/v1/service-jobs/" + job_id)
                if job.get("status") in terminal:
                    states[job_id] = job
                    del jobs[job_id]
            if not jobs:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("SERVICE_EXECUTION_STILL_ACTIVE")
            time.sleep(2)

        for job_id, job in states.items():
            if job["status"] == "uncertain":
                worker_id = job.get("worker_id")
                if not worker_id:
                    raise RuntimeError("SERVICE_PREVIOUS_EXECUTION_NOT_STOPPED")
                self.wait_for_service_worker_release(job_id, worker_id)
                response = self.api(
                    "POST",
                    "/v1/service-jobs/" + job_id + "/cancel",
                    {"previous_execution_stopped": True},
                    key="matrix-recovery-cancel-" + job_id,
                )
                if response.get("status") != "cancelled":
                    raise RuntimeError("SERVICE_RECOVERY_CANCEL_FAILED")
                self.record(
                    "service_recovery_cancelled",
                    case=items_by_job.get(job_id, {}).get("name", "unknown"),
                    job_id=job_id,
                    previous_execution_stopped=True,
                )
            elif job["status"] in {"queued", "blocked"}:
                response = self.api(
                    "POST",
                    "/v1/service-jobs/" + job_id + "/cancel",
                    {},
                    key="matrix-recovery-cancel-" + job_id,
                )
                if response.get("status") != "cancelled":
                    raise RuntimeError("SERVICE_RECOVERY_CANCEL_FAILED")

    def process_service_batch(self, items):
        failures = []
        for item in items:
            if item["product"] == "kvm":
                continue
            try:
                self.wait_service(item["job_id"])
            except (RuntimeError, AssertionError) as error:
                reason = (
                    str(error)
                    if type(error) in {RuntimeError, AssertionError}
                    and re.fullmatch(r"[A-Z0-9_.:-]{1,120}", str(error))
                    else type(error).__name__
                )
                failures.append((item, reason))
                self.record("service_case_error", case=item["name"], error=reason)
        if not failures:
            return

        self.settle_service_batch(items)
        for item, _reason in failures:
            item["service_failure"] = True
        # Attempt every exact test instance even if one cleanup fails. The
        # parallel helper collects failures only after all submitted cleanups
        # have completed, avoiding a leaked sibling VM on the first error.
        self.parallel(self.cleanup, items)
        self.post_cleanup(items)
        self.idle()
        raise RuntimeError("SERVICE_BATCH_FAILED_AFTER_CLEANUP")

    def cleanup(self, item):
        before = self.api("GET", "/v1/instances/" + item["instance_id"])
        vmid = before.get("vmid")
        if item.get("vmid") is not None:
            assert vmid == item["vmid"], "DELETE_IDENTITY_NOT_CONFIRMED"
        assert isinstance(vmid, int) and self.vmid_range[0] <= vmid <= self.vmid_range[1], (
            "DELETE_VMID_NOT_ASSIGNED"
        )
        item["vmid"] = vmid
        assert (
            before.get("name") == item["name"]
            and before.get("image_id") == item["image"]
            and before.get("plan_id") == item["plan"]
            and before.get("status") == "ready"
        ), "DELETE_IDENTITY_NOT_CONFIRMED"
        allocation = before.get("network_allocation")
        if self.profile == "prod_static":
            assert (
                isinstance(allocation, dict)
                and allocation.get("profile") == "prod_static"
                and allocation.get("status") == "allocated"
                and allocation.get("gateway") == "190.0.231.1"
                and isinstance(allocation.get("address"), str)
            ), "DELETE_IP_ALLOCATION_NOT_CONFIRMED"
            address = allocation["address"].split("/")[0]
            assert address.startswith("190.0.231."), "DELETE_IP_ALLOCATION_NOT_CONFIRMED"
            if item.get("address") is not None:
                assert item["address"] == address, "DELETE_IDENTITY_NOT_CONFIRMED"
            item["address"] = address
        if item.get("product") != "kvm":
            service = self.api(
                "GET", "/v1/service-instances/" + item["service_id"]
            )
            assert (
                service.get("service_id") == item["service_id"]
                and service.get("instance_id") == item["instance_id"]
                and service.get("application_id") == item["product"]
            ), "DELETE_SERVICE_IDENTITY_NOT_CONFIRMED"
        self.wait_operation(
            self.api(
                "POST",
                "/v1/instances/" + item["instance_id"] + "/power/shutdown",
                key="shutdown-" + item["name"],
            )
        )
        delete_path = (
            "/v1/service-instances/" + item["service_id"]
            if item["product"] != "kvm"
            else "/v1/instances/" + item["instance_id"]
        )
        deleted_operation = self.api(
            "DELETE", delete_path, key="delete-" + item["name"]
        )
        item["delete_operation_id"] = deleted_operation["operation_id"]
        self.wait_operation(deleted_operation)
        self.check(item, "E01", "instancia eliminada por DELETE canónico")
        assert not any(
            int(v.get("vmid", 0)) == item["vmid"] for v in self.proxmox.inventory()
        )
        self.check(item, "E02", "VM ausente del inventario Proxmox del clúster MNG")
        state = self.api("GET", "/v1/instances/" + item["instance_id"])
        assert state["status"] == "deleted"
        self.check(item, "E03", "API conserva auditoría y estado deleted para la identidad probada")
        if self.profile == "lab_dhcp":
            assert state["network_allocation"] is None
            self.check(item, "E04", "no había reserva IPAM que liberar en lab_dhcp")
            return
        assert state["network_allocation"]["status"] == "released"
        # La misma dirección debe quedar ausente de NetBox, sin tocar registros ajenos.
        headers = {"Authorization": "Bearer " + self.env["IACP_NETBOX_TOKEN"]}
        req = Request(
            self.env["IACP_NETBOX_URL"].rstrip("/")
            + "/ipam/ip-addresses/?address="
            + item["address"]
            + "/24",
            headers=headers,
        )
        with urlopen(req, timeout=30) as f:
            assert json.load(f)["count"] == 0
        self.check(item, "E04", "IP liberada en API y ausente de NetBox")
        events = self.api(
            "GET",
            "/v1/events?operation_id=" + item["delete_operation_id"] + "&sort=asc&limit=100",
        ).get("events", [])
        assert any(
            row.get("kind") in {"logical.delete.completed", "instance.delete.completed"}
            and row.get("status") == "succeeded"
            for row in events
        ), "DELETE_AUDIT_EVENT_MISSING"
        self.check(item, "E06", "operación y evento de auditoría DELETE registrados")

    def post_cleanup(self, items):
        targets = [
            {
                "instance_id": item["instance_id"],
                "vmid": item["vmid"],
                "operation_id": item["operation_id"],
            }
            for item in items
        ]
        script = """
import json,sys
from pathlib import Path
from api.db import database
targets=json.loads(sys.argv[1])
def state_summary(root,vmid):
    if not root:return {'checked':False,'resources':None,'target_present':None}
    paths=list(Path(root).rglob('terraform.tfstate')) if Path(root).exists() else []
    if not paths:return {'checked':False,'resources':None,'target_present':None}
    resources=[]
    def collect(module):
        resources.extend(module.get('resources',[]))
        for child in module.get('child_modules',[]):collect(child)
    for path in paths:
        data=json.loads(path.read_text())
        if data.get('resources'):resources.extend(data['resources'])
        if data.get('outputs') is not None and data.get('terraform_version'):
            root_module=(data.get('values') or {}).get('root_module')
            if root_module:collect(root_module)
    present=False
    for resource in resources:
        values=resource.get('instances',[{}])[0].get('attributes',{}) if resource.get('instances') else resource.get('values',{})
        candidate=values.get('vm_id',values.get('vmid')) if isinstance(values,dict) else None
        if str(candidate)==str(vmid):present=True
    return {'checked':True,'resources':len(resources),'target_present':present}
result={'instances':[]}
with database() as db:
    result['active_operations']=db.execute("SELECT COUNT(*) AS n FROM operations WHERE status IN ('pending','running','planning','planned','applying','destroying','powering','uncertain')").fetchone()['n']
    result['active_service_jobs']=db.execute("SELECT COUNT(*) AS n FROM service_jobs WHERE status IN ('queued','running','uncertain')").fetchone()['n']
    result['advisory_locks']=db.execute("SELECT COUNT(*) AS n FROM pg_locks WHERE locktype='advisory'").fetchone()['n']
    for target in targets:
        lease=db.execute("SELECT status FROM vmid_leases WHERE vmid=?",(target['vmid'],)).fetchone()
        instance=db.execute("SELECT operation_id,status FROM instances WHERE instance_id=?",(target['instance_id'],)).fetchone()
        active=db.execute("SELECT COUNT(*) AS n FROM operations WHERE instance_id=? AND status IN ('pending','running','planning','planned','applying','destroying','powering','uncertain')",(target['instance_id'],)).fetchone()['n']
        jobs=db.execute("SELECT COUNT(*) AS n FROM service_jobs j JOIN service_instances s ON s.service_id=j.service_id WHERE s.instance_id=? AND j.status IN ('queued','running','uncertain')",(target['instance_id'],)).fetchone()['n']
        workspace=db.execute("SELECT workspace_path FROM operations WHERE operation_id=?",(target['operation_id'],)).fetchone()
        tf=state_summary(workspace['workspace_path'] if workspace else None,target['vmid'])
        result['instances'].append({'instance_id':target['instance_id'],'vmid':target['vmid'],'lease_status':lease['status'] if lease else None,'instance_status':instance['status'] if instance else None,'active_operations':active,'active_service_jobs':jobs,'terraform':tf})
print(json.dumps(result,sort_keys=True))
"""
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.api_container,
                "/app/.venv/bin/python",
                "-",
                json.dumps(targets),
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError("POST_DELETE_STATE_QUERY_FAILED")
        summary = json.loads(result.stdout)
        assert summary["active_operations"] == 0, "POST_DELETE_OPERATIONS_ACTIVE"
        assert summary["active_service_jobs"] == 0, "POST_DELETE_SERVICE_JOBS_ACTIVE"
        assert summary["advisory_locks"] == 0, "POST_DELETE_ADVISORY_LOCKS_ACTIVE"
        for item, state in zip(items, summary["instances"], strict=True):
            assert state["lease_status"] == "released", "VMID_LEASE_NOT_RELEASED"
            assert state["instance_status"] == "deleted"
            assert state["active_operations"] == 0 and state["active_service_jobs"] == 0
            assert state["terraform"]["checked"]
            assert state["terraform"]["resources"] == 0
            assert not state["terraform"]["target_present"]
            self.check(item, "E03", "lease VMID liberado y state Terraform sin recursos activos")
            self.check(item, "E05", "sin operaciones o trabajos huérfanos; locks advisory en cero")
        self.record(
            "post_cleanup",
            instance_count=len(items),
            active_operations=summary["active_operations"],
            active_service_jobs=summary["active_service_jobs"],
            advisory_locks=summary["advisory_locks"],
            terraform_active_resources=sum(
                row["terraform"]["resources"] for row in summary["instances"]
            ),
        )

    def verify_secrets_absent(self, items):
        for path in self.folder.rglob("*"):
            if not path.is_file():
                continue
            contents = path.read_bytes()
            if any(secret.encode() in contents for secret in self.secrets):
                raise RuntimeError("TEST_SECRET_FOUND_IN_EVIDENCE")
        listed = subprocess.run(
            [
                "docker",
                "ps",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + self.project,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        containers = listed.stdout.split()
        if listed.returncode or not containers:
            raise RuntimeError("RUNTIME_LOG_SCAN_UNAVAILABLE")
        for container in containers:
            logs = subprocess.run(
                ["docker", "logs", "--since", self.run_started_at, container],
                capture_output=True,
                timeout=120,
            )
            if logs.returncode:
                raise RuntimeError("TEST_LOG_SCAN_UNAVAILABLE")
            if any(secret.encode() in logs.stdout or secret.encode() in logs.stderr for secret in self.secrets):
                raise RuntimeError("TEST_SECRET_FOUND_IN_RUNTIME_LOG")
        for item in items:
            self.check(item, "B05", "credenciales efímeras ausentes de logs y evidencias")

    def close(self):
        shutil.rmtree(self.key_directory, ignore_errors=True)

    def run(self):
        planned = cases(
            self.args.category,
            self.args.images,
            self.args.cycle,
            include_controls=not self.args.exact_cases,
        )
        if self.args.exact_cases:
            exact_services = (
                self.args.category == "services"
                and self.args.images == ["ubuntu-2404"]
                and self.profile == "prod_static"
                and len(planned) == 8
            )
            exact_kvm = (
                self.args.category == "kvm"
                and self.args.images in (["fedora-45"], ["debian-13"])
                and self.profile == "prod_static"
                and len(planned) == 16
                and all(
                    sum(case["plan"] == f"kvm-{plan}" for case in planned) == 4
                    for plan in range(1, 5)
                )
            )
            if not (exact_services or exact_kvm):
                raise RuntimeError("EXACT_MATRIX_CASES_INVALID")
        if self.args.production and (
            not self.args.exact_cases
            or self.args.category != "services"
            or self.args.images != ["ubuntu-2404"]
        ):
            raise RuntimeError("PRODUCTION_MATRIX_REQUIRES_EXACT_UBUNTU_SERVICES")
        existing_names = []
        offset = 0
        while True:
            page = self.api(
                "GET",
                f"/v1/instances?limit=200&offset={offset}&include_deleted=true",
            )
            existing_names.extend(
                row["name"]
                for row in page.get("instances", [])
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            )
            offset += len(page.get("instances", []))
            if offset >= page.get("total", 0) or not page.get("instances"):
                break
        self.matrix_names = allocate_matrix_names(
            self.args.category, planned, existing_names
        )
        self.record(
            "start",
            category=self.args.category,
            network_profile=self.profile,
            cycle=self.args.cycle,
            revision=self.env["IACP_IMAGE_TAG"],
            harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            cases=sum(not case["control"] for case in planned),
            controls=sum(bool(case["control"]) for case in planned),
            images=sorted({case["image"] for case in planned}),
            name_prefix=(
                "kvm-prueba"
                if self.args.category == "kvm"
                else "kvm-service-<producto>"
                if self.args.category == "services"
                else "kvm-agent-<producto>"
            ),
        )
        self.verify_network_profile()
        if self.args.preflight_only:
            self.idle()
            self.record(
                "PREFLIGHT",
                result="PASS",
                production=self.args.production,
                no_instances_created=True,
            )
            return
        indexed = list(enumerate(planned))
        if self.args.production and self.args.cycle == 1:
            pilot_indexes = (0, next(i for i, item in enumerate(planned) if item["product"] == "nextcloud"))
            pilot_batches = [
                [(index, dict(planned[index], pilot=True))]
                for index in pilot_indexes
            ]
            remaining = [pair for pair in indexed if pair[0] not in pilot_indexes]
            batches = pilot_batches + [remaining[index : index + 6] for index in range(0, len(remaining), 6)]
        else:
            batches = [indexed[index : index + 6] for index in range(0, len(indexed), 6)]
        for batch in batches:
            self.idle()
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                items = list(
                    pool.map(
                        lambda pair: self.create(*pair),
                        [(index + 1, case) for index, case in batch],
                    )
                )
            self.record(
                "injected",
                instances=[
                    {k: i[k] for k in ["name", "instance_id", "operation_id"]}
                    for i in items
                ],
            )
            peak = 0
            for _ in range(900):
                snapshot = self.api("GET", "/v1/observability")
                peak = max(peak, snapshot["active_operations"])
                states = [
                    self.api("GET", "/v1/operations/" + i["operation_id"])["status"]
                    for i in items
                ]
                if any(s in {"failed", "uncertain"} for s in states):
                    raise RuntimeError("PROVISION_FAILED_OR_UNCERTAIN")
                if all(s in {"completed", "succeeded"} for s in states):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("BATCH_TIMEOUT")
            expected_peak = min(6, len(items))
            assert peak == expected_peak, "BATCH_CONCURRENCY_NOT_OBSERVED"
            if len(items) == 6:
                assert peak == 6, "SIX_SIMULTANEOUS_NOT_OBSERVED"
            self.record("concurrency", simultaneous_peak=peak, batch_size=len(items))
            self.process_service_batch(items)
            self.parallel(self.verify, items)
            self.parallel(self.cleanup, items)
            self.post_cleanup(items)
            self.idle()
        self.verify_secrets_absent(self.created_items)
        self.record(
            "COMPLETE",
            result="PASS",
            cases=sum(not case["control"] for case in planned),
            controls=sum(bool(case["control"]) for case in planned),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path("/etc/iacp/stacks/dev.env"))
    parser.add_argument(
        "--production",
        action="store_true",
        help="Apunta exclusivamente a iac.cyberfuel.com con prod.env y Prod_Static; requiere autorización operativa.",
    )
    parser.add_argument(
        "--netbox-env",
        type=Path,
        default=Path("/etc/iacp/stacks/dev.env"),
        help="Origen del token NetBox compartido autorizado para la matriz de producción.",
    )
    parser.add_argument(
        "--category", choices=["kvm", "services", "agents"], required=True
    )
    parser.add_argument("--cycle", type=int, required=True)
    parser.add_argument(
        "--network-profile", choices=["lab_dhcp", "prod_static"], default="prod_static"
    )
    parser.add_argument("--run", required=True)
    parser.add_argument(
        "--service-timeout-seconds",
        type=int,
        default=14400,
        help="Tiempo máximo por servicio para instalar dependencias e imágenes OCI (60–21600).",
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--exact-cases",
        action="store_true",
        help="Ejecuta el alcance exacto: ocho servicios Ubuntu o 16 KVM de Fedora/Debian, sin controles extra.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Comprueba stack, seis workers, servicios, catálogo, MNG, WAN e IPAM sin crear recursos.",
    )
    parser.add_argument(
        "--images",
        nargs="+",
        choices=["ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10", "fedora-45", "debian-13"],
        help="Limita la matriz a imágenes concretas; servicios separa Ubuntu y AlmaLinux por ciclo.",
    )
    args = parser.parse_args()
    if not 1 <= args.cycle <= required_cycles(args.category):
        parser.error(
            "KVM y servicios admiten ciclos 1–4; agentes admiten ciclos 1–2"
        )
    if not args.run.isdigit() or len(args.run) > 14:
        parser.error("run debe ser una marca numérica de hasta 14 dígitos")
    matrix = Matrix(args)
    try:
        matrix.run()
    except Exception as error:
        # No serializar errores arbitrarios: pueden contener credenciales HTTP.
        matrix.record(
            "FAIL" if isinstance(error, AssertionError) else "BLOCKED",
            reason=str(error)
            if type(error) in {AssertionError, RuntimeError}
            else type(error).__name__,
            created_instances=[
                {k: item.get(k) for k in ("name", "instance_id", "operation_id", "vmid", "status")}
                for item in matrix.created_items
            ],
        )
        sys.exit(1)
    finally:
        matrix.close()
