import json
import logging
import os
import time

from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .api import health, instances, jobs, operations, terraform, applies, power
from .security import authorize_docs


def create_app() -> FastAPI:
    application = FastAPI(
        title="IACP Control Plane", version="0.6.0",
        docs_url=None, redoc_url=None, openapi_url=None,
        servers=[{"url": "https://iac.cyberfuel.com", "description": "API oficial; acceso autorizado"}],
        description=(
            "Inventario por tenant y operaciones asíncronas. Simulación por defecto y "
            "Terraform con planes aprobados y doble autorización; ejecución real deshabilitada por defecto."
        ),
    )
    @application.middleware("http")
    async def trace_request(request, call_next):
        from uuid import uuid4
        request.state.request_id = str(uuid4())
        started = time.monotonic()
        if request.method not in {"GET", "HEAD", "OPTIONS"} and os.environ.get("IACP_ADMISSION_ENABLED", "1") != "1":
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
                                   swagger_ui_parameters={"persistAuthorization": False})

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

    from .terraform.config import PlanError

    @application.exception_handler(PlanError)
    def executor_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": error.code})

    @application.exception_handler(SQLAlchemyError)
    def persistence_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "Persistence unavailable"})

    return application


app = create_app()
