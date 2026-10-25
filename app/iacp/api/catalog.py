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
    cloud_init: bool
    qemu_guest_agent: bool
    root_access: bool
    ssh_access: bool
    minimum_requirements: MinimumRequirements
    compatible_plans: list[str]
    authorized: bool
    availability: Availability


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
    summary="Consultar Golden Images y su autorización",
    description="La imagen habilitada ubuntu-2404 procede de la certificación histórica. "
    "root_access/ssh_access describen esa capacidad histórica, no certifican G6. "
    "availability distingue habilitación, permiso y ejecución segura; no publica VMID, nodo ni almacenamiento interno.")
def images(response: Response, principal: Annotated[Principal, Depends(authorize)],
           session: Annotated[Session,Depends(get_session)]):
    catalog_headers(response)
    return images_for(principal.tenant_id,session)
