import json
import logging
import os
import re
import time

from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from .api import health, instances, jobs, operations, terraform, applies, power, deletion, lab, observability
from .security import authorize_docs


def create_app() -> FastAPI:
    application = FastAPI(
        title="IACP Orchestrator", version="0.10.0",
        docs_url=None, redoc_url=None, openapi_url=None,
        servers=[{"url": "https://iac.cyberfuel.com", "description": "API oficial; acceso autorizado"}],
        description=(
            "IACP Orchestrator administra instancias KVM, operaciones asíncronas y Lifecycle por tenant. "
            "El componente **IaC Worker** aprovisiona recursos con perfiles y cuotas autorizados. "
            "API corporativa privada por LAN/VPN. El backend CRM consume el contrato neutral de instancias y operaciones.\n\n"
            "**Acceso:** el login HTTP Basic de esta documentación usa una identidad docs. "
            "Para ejecutar consultas y comandos, pulse **Authorize** e introduzca SOLO el token Bearer "
            "del tenant, sin el prefijo Bearer. La contraseña docs no autoriza llamadas a /v1.\n\n"
            "**Roles:** CRM/reader consulta inventario, operaciones y energía; operator crea y solicita "
            "energía; approver autoriza planes y debe ser distinto del solicitante.\n\n"
            "**Autoservicio:** consultar GET /v1/profiles y GET /v1/lab/policies. Crear con network_profile y "
            "self_service_policy_id aprobados admite una VM nueva y el IaC Worker procesa el plan verificado por delegación. "
            "Los perfiles de autoservicio exigen política; perfiles históricos conservan revisión independiente de cada plan. "
            "HTTP 202 acredita admisión, no una VM lista. "
            "Seguir operation_id hasta completed/failed/uncertain. Mantener Idempotency-Key en reintentos.\n\n"
            "**Identificadores:** instance_id es el UUID estable de la instancia; VMID es un identificador interno asignado por IACP. "
            "operation_id identifica una intención asíncrona; X-Request-ID correlaciona cada petición HTTP. "
            "Idempotency-Key identifica una intención dentro del tenant: misma clave y mismo cuerpo conservan IDs; otro cuerpo devuelve 409. "
            "VMID, plantilla y direccionamiento proceden de perfiles autorizados; node_preference valida un destino admitido u omitir para selección automática. "
            "Los perfiles DHCP omiten IPAM y no esperan a descubrir la IP para completar creación. "
            "Consultar ip_addresses/network_observation y reintentar network/discover cuando arranque el invitado. "
            "Con admisión cerrada los comandos devuelven 503; Lifecycle tiene "
            "su habilitación y certificación independientes. Apagado ordenado: power/shutdown, sin force. "
            "Running no certifica Guest Agent, Cloud-Init ni guest_ready.\n\n"
            "**DELETE:** deshabilitado por defecto. Solo UUID descartable en allowlist, VMID >=30000, "
            "plan destroy verificado y aprobación independiente o delegación vigente para una VM creada bajo esa política. "
            "Con política, DELETE con cuerpo {} prepara y ejecuta el único plan autorizado; exige VM apagada y sin locks. "
            "VM 104 y VM 106 están protegidas. Las operaciones inciertas solo se reconcilian por observación."
        ),
        openapi_tags=[
            {"name": "health", "description": "Liveness y readiness; no crean infraestructura."},
            {"name": "instances", "description": "Inventario del tenant. Crear requiere operator; perfil DHCP autorizado o asignación estática IPAM previa."},
            {"name": "operations", "description": "Seguimiento asíncrono y eventos auditados por operation_id."},
            {"name": "lab-administration", "description": "Administración de políticas de laboratorio: propuesta, revisión independiente, vigencia, cuotas y revocación. No integra sistemas comerciales."},
            {"name": "provisioning-administration", "description": "Administración interna del IaC Worker: planes e intentos. Rutas históricas conservadas; el CRM usa instances y operations."},
            {"name": "provisioning-approvals", "description": "Administración interna del IaC Worker: aprobación independiente y ejecución de un único plan guardado, o delegación auditable."},
            {"name": "kvm-lifecycle", "description": "Lifecycle Worker: energía de KVM propias; exige contrato de propiedad y habilitación."},
            {"name": "disposable-deletion", "description": "Plan, aprobación y DELETE de laboratorio; nunca recursos existentes protegidos."},
            {"name": "observability", "description": "Contrato IaCoC de eventos versionados y diagnóstico por tenant; no ejecuta aprovisionamiento."},
        ],
    )
    @application.middleware("http")
    async def trace_request(request, call_next):
        from uuid import uuid4
        request.state.request_id = str(uuid4())
        from .services.tracing import correlation_id
        trace_token = correlation_id.set(request.state.request_id)
        started = time.monotonic()
        # These authenticated handlers cannot submit infrastructure mutations:
        # cancel refuses a consumed command; recover accepts only observation.
        bookkeeping = request.method == 'POST' and re.fullmatch(
            r'/v1/instances/[a-f0-9-]{36}/(?:terraform/deletions/[a-f0-9-]{36}/(?:cancel|recover)|terraform/applies/[a-f0-9-]{36}/(?:cancel|recover|resolve-absence)|network/discover|reconcile)', request.url.path)
        # Policy administration prepares/revokes delegation without submitting
        # infrastructure work. It must be usable before activate-lab opens gates.
        bookkeeping = bookkeeping or (request.method == 'POST' and re.fullmatch(
            r'/v1/lab/policies(?:/[a-f0-9-]{36}/(?:approve|renew|revoke))?',request.url.path))
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
        correlation_id.reset(trace_token)
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
    application.include_router(lab.router)
    application.include_router(observability.router)

    @application.exception_handler(RequestValidationError)
    def invalid_request(request, error):
        # Pydantic errors otherwise echo rejected inputs (including secrets and
        # unknown fields). Keep locations/types, never input, context or values.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(item['loc']), "type": item['type'], "msg": "Invalid request value"}
            for item in error.errors()]})

    from .terraform.config import PlanError

    @application.exception_handler(PlanError)
    def executor_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": error.code})

    @application.exception_handler(SQLAlchemyError)
    def persistence_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "Persistence unavailable"})

    return application


app = create_app()
