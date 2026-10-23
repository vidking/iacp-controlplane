import json
import logging
import os
import re
import time

from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .api import health, instances, jobs, operations, terraform, applies, power, deletion
from .security import authorize_docs


def create_app() -> FastAPI:
    application = FastAPI(
        title="IACP Control Plane", version="0.8.0",
        docs_url=None, redoc_url=None, openapi_url=None,
        servers=[{"url": "https://iac.cyberfuel.com", "description": "API oficial; acceso autorizado"}],
        description=(
            "API corporativa privada por LAN/VPN.\n\n"
            "**Acceso:** el login HTTP Basic de esta documentación usa una identidad docs. "
            "Para ejecutar consultas y comandos, pulse **Authorize** e introduzca SOLO el token Bearer "
            "del tenant, sin el prefijo Bearer. La contraseña docs no autoriza llamadas a /v1.\n\n"
            "**Roles:** CRM/reader consulta inventario, operaciones y energía; operator crea y solicita "
            "energía; approver autoriza planes y debe ser distinto del solicitante.\n\n"
            "**Flujo real:** crear prepara un plan; consultar la ejecución Terraform, revisar y aprobar "
            "su SHA-256 y aplicar el plan autorizado. HTTP 202 acredita admisión, no una VM lista. "
            "Seguir operation_id hasta completed/failed/uncertain. Mantener Idempotency-Key en reintentos.\n\n"
            "VMID, nodo, plantilla y direccionamiento proceden de perfiles autorizados del servidor; no se aceptan en el payload. "
            "Los perfiles DHCP omiten IPAM y no esperan a descubrir la IP para completar creación. "
            "Consultar ip_addresses/network_observation y reintentar network/discover cuando arranque el invitado. "
            "Con admisión cerrada los comandos devuelven 503; Lifecycle tiene "
            "su habilitación y certificación independientes. Apagado ordenado: power/shutdown, sin force. "
            "Running no certifica Guest Agent, Cloud-Init ni guest_ready.\n\n"
            "**DELETE:** deshabilitado por defecto. Solo UUID descartable en allowlist, VMID >=30000, "
            "plan destroy revisado y aprobación independiente. VM 104 y VM 106 están protegidas."
        ),
        openapi_tags=[
            {"name": "health", "description": "Liveness y readiness; no crean infraestructura."},
            {"name": "instances", "description": "Inventario del tenant. Crear requiere operator; perfil DHCP autorizado o asignación estática IPAM previa."},
            {"name": "operations", "description": "Seguimiento asíncrono y eventos auditados por operation_id."},
            {"name": "terraform", "description": "Estado, intento y SHA del plan; cancelar/reintentar no ejecuta Apply."},
            {"name": "terraform-apply", "description": "Revisión independiente y Apply de un único plan guardado."},
            {"name": "kvm-lifecycle", "description": "Energía de KVM propias; exige contrato de propiedad y flag Lifecycle."},
            {"name": "disposable-deletion", "description": "Plan, aprobación y DELETE de laboratorio; nunca recursos existentes protegidos."},
        ],
    )
    @application.middleware("http")
    async def trace_request(request, call_next):
        from uuid import uuid4
        request.state.request_id = str(uuid4())
        started = time.monotonic()
        # These authenticated handlers cannot submit infrastructure mutations:
        # cancel refuses a consumed command; recover accepts only observation.
        bookkeeping = request.method == 'POST' and re.fullmatch(
            r'/v1/instances/[a-f0-9-]{36}/(?:terraform/deletions/[a-f0-9-]{36}/(?:cancel|recover)|terraform/applies/[a-f0-9-]{36}/(?:cancel|recover|resolve-absence)|network/discover)', request.url.path)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not bookkeeping and os.environ.get("IACP_ADMISSION_ENABLED", "1") != "1":
            response = JSONResponse(status_code=503, content={"detail": "Admission drained"}, headers={"Retry-After": "60"})
        else:
            try:
                response = await call_next(request)
            except Exception:
                # Correlate unexpected failures without logging request bodies,
                # query strings, credential values or provider error messages.
                response = JSONResponse(status_code=500, content={"detail": "Internal error"})
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        logging.getLogger("uvicorn.error").info(json.dumps({
            "event": "http_request", "request_id": request.state.request_id,
            "method": request.method, "route": getattr(route, "path", "unmatched"),
            "status": response.status_code, "duration_ms": round((time.monotonic() - started) * 1000),
        }))
        return response

    @application.get("/docs", include_in_schema=False, dependencies=[Depends(authorize_docs)])
    def docs():
        return get_swagger_ui_html(openapi_url="/openapi.json", title="IACP API",
                                   swagger_ui_parameters={"persistAuthorization": False, "docExpansion": "none",
                                                          "displayRequestDuration": True, "filter": True,
                                                          "tryItOutEnabled": False})

    @application.get("/openapi.json", include_in_schema=False, dependencies=[Depends(authorize_docs)])
    def contract():
        return JSONResponse(application.openapi())

    application.include_router(health.router)
    application.include_router(jobs.router)
    application.include_router(instances.router)
    application.include_router(operations.router)
    application.include_router(terraform.router)
    application.include_router(applies.router)
    application.include_router(power.router)
    application.include_router(deletion.router)

    from .terraform.config import PlanError

    @application.exception_handler(PlanError)
    def executor_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": error.code})

    @application.exception_handler(SQLAlchemyError)
    def persistence_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "Persistence unavailable"})

    return application


app = create_app()
