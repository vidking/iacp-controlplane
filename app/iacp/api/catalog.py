"""Catálogos de lectura con la identidad Bearer y el tenant vigentes."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..catalog import CATALOG_VERSION, catalog_digest
from ..db import get_session
from ..schemas import ErrorResponse
from ..security import Principal, authorize
from ..services.catalog import images_for, plans_for

router = APIRouter(tags=["instances"], responses={401: {"model": ErrorResponse},
    403: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})


class Availability(BaseModel):
    available: bool
    capacity_verified: bool
    reasons: list[str]


class PlanResponse(BaseModel):
    plan_id: str
    name: str
    vcpus: int
    memory_mb: int
    disk_gb: int
    availability: Availability


class MinimumRequirements(BaseModel):
    vcpus: int
    memory_mb: int
    disk_gb: int


class ImageResponse(BaseModel):
    image_id: str
    operating_system: str
    version: str
    status: Literal["pendiente", "certificada", "habilitada", "retirada"]
    cloud_init: bool | None
    qemu_guest_agent: bool | None
    root_access: bool | None
    ssh_access: bool | None
    minimum_requirements: MinimumRequirements | None
    compatible_plans: list[str]
    authorized: bool
    availability: Availability


IMAGE_RESPONSE_EXAMPLE = [
    {"image_id": image_id, "operating_system": system, "version": version,
     "status": "habilitada" if enabled else "pendiente",
     "cloud_init": True if enabled else None, "qemu_guest_agent": True if enabled else None,
     "root_access": True if enabled else None, "ssh_access": True if enabled else None,
     "minimum_requirements": {"vcpus": 1, "memory_mb": 512, "disk_gb": 20} if enabled else None,
     "compatible_plans": ["kvm-1", "kvm-2", "kvm-3", "kvm-4"] if enabled else [],
     "authorized": enabled, "availability": {"available": False, "capacity_verified": False,
         "reasons": ["PHYSICAL_CAPACITY_NOT_VERIFIED" if enabled else "IMAGE_CERTIFICATION_PENDING"]}}
    for image_id, system, version, enabled in [
        ("ubuntu-2404", "Ubuntu", "24.04", True), ("ubuntu-2604", "Ubuntu", "26.04", False),
        ("almalinux-9", "AlmaLinux", "9", False), ("almalinux-10", "AlmaLinux", "10", False)]
]


def catalog_headers(response):
    response.headers["X-IACP-Catalog-Version"] = CATALOG_VERSION
    response.headers["X-IACP-Catalog-SHA256"] = catalog_digest()


@router.get("/v1/plans", response_model=list[PlanResponse], operation_id="listKVMPlans",
    summary="Consultar los cuatro planes KVMHOST-54",
    description="Catálogo versionado por cabeceras X-IACP-Catalog-Version y X-IACP-Catalog-SHA256. "
    "vcpus, memory_mb y disk_gb expresan CPU, MiB y GiB. La disponibilidad consulta límites y uso del tenant; "
    "no reserva recursos. En modo comercial verifica capacidad física; en modo histórico informa que G no está activo.")
def plans(response: Response, principal: Annotated[Principal, Depends(authorize)],
          session: Annotated[Session, Depends(get_session)]):
    catalog_headers(response)
    return plans_for(session, principal.tenant_id)


@router.get("/v1/images", response_model=list[ImageResponse], operation_id="listGoldenImages",
    responses={200: {"description": "Inventario autorizado; consultar disponibilidad efectiva antes de crear",
        "content": {"application/json": {"examples": {"inventario_pendiente": {
            "summary": "Cuatro imágenes; ejemplo de capacidad todavía no certificada",
            "value": IMAGE_RESPONSE_EXAMPLE}}}}}},
    summary="Consultar Golden Images y su autorización",
    description="Inventario: ubuntu-2404, ubuntu-2604, almalinux-9 y almalinux-10. "
    "Solo ubuntu-2404 está habilitada por su certificación histórica; las otras permanecen pendientes. "
    "Capacidades null y compatible_plans vacío indican pruebas pendientes, no incompatibilidad demostrada. "
    "root_access/ssh_access describen esa capacidad histórica, no certifican G6. "
    "availability distingue habilitación, permiso y ejecución segura; no publica VMID, nodo ni almacenamiento interno.")
def images(response: Response, principal: Annotated[Principal, Depends(authorize)],
           session: Annotated[Session,Depends(get_session)]):
    catalog_headers(response)
    return images_for(principal.tenant_id,session)
