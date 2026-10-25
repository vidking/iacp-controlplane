"""Catálogo KVMHOST-54 versionado; no reserva recursos ni amplía cuotas."""
from dataclasses import dataclass
from hashlib import sha256
import json


CATALOG_VERSION = "KVMHOST-54.3"


@dataclass(frozen=True)
class Plan:
    plan_id: str
    name: str
    vcpus: int
    memory_mb: int
    disk_gb: int


PLANS = (
    Plan("kvm-1", "KVM 1", 1, 2048, 50),
    Plan("kvm-2", "KVM 2", 2, 4096, 100),
    Plan("kvm-3", "KVM 3", 4, 8192, 200),
    Plan("kvm-4", "KVM 4", 8, 16384, 400),
)


@dataclass(frozen=True)
class GoldenImage:
    image_id: str
    operating_system: str
    version: str
    status: str
    template_vm_id: int
    source_node: str
    source_storage: str
    cloud_init: bool
    qemu_guest_agent: bool
    root_access: bool
    ssh_access: bool
    minimum_vcpus: int
    minimum_memory_mb: int
    minimum_disk_gb: int


# Procedencia comprobada en VM 104 el 24/10/2026, America/Costa_Rica.
# Metadatos internos: VMID/nodo/storage no forman parte de GET /v1/images.
# La certificación histórica de root NO certifica el requisito G6 fuera del state.
IMAGES = (GoldenImage("ubuntu-2404", "Ubuntu", "24.04", "habilitada", 29000,
    "mng01", "DS_CYBERFUEL", True, True, True, True, 1, 512, 20),)


@dataclass(frozen=True)
class PendingGoldenImage:
    """Registro interno incompleto: no presume procedencia ni compatibilidad."""
    image_id: str
    operating_system: str
    version: str
    template_vm_id: int
    source_node: str
    source_storage: str
    status: str = "pendiente"


# Identidad comunicada por Infraestructura el 25/10/2026, America/Costa_Rica.
# Inventario Proxmox de solo lectura comprobado el 25/10/2026: plantillas,
# mng01 y DS_CYBERFUEL. La presencia no certifica bootstrap o filesystem.
PENDING_IMAGES = (
    PendingGoldenImage("ubuntu-2604", "Ubuntu", "26.04", 29001, "mng01", "DS_CYBERFUEL"),
    PendingGoldenImage("almalinux-9", "AlmaLinux", "9", 29010, "mng01", "DS_CYBERFUEL"),
    PendingGoldenImage("almalinux-10", "AlmaLinux", "10", 29011, "mng01", "DS_CYBERFUEL"),
)


def resolve_plan(plan_id: str) -> Plan:
    for plan in PLANS:
        if plan.plan_id == plan_id:
            return plan
    raise ValueError("PLAN_NOT_FOUND")


def resolve_image(image_id: str) -> GoldenImage:
    for image in IMAGES:
        if image.image_id == image_id and image.status == "habilitada":
            return image
    raise ValueError("IMAGE_NOT_AVAILABLE")


def catalog_digest() -> str:
    """Digest determinista del producto, sin autorización ni capacidad dinámica."""
    from dataclasses import asdict
    data = {"version": CATALOG_VERSION, "plans": [asdict(p) for p in PLANS],
            "images": [asdict(i) for i in IMAGES],
            "pending_images": [asdict(i) for i in PENDING_IMAGES]}
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
