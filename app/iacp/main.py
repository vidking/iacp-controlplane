import json
import logging
import os
import re
import time

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from .api import health, instances, jobs, operations, terraform, applies, power, deletion, lab, observability, catalog
from .security import authorize_docs, authorize_admin_docs
from .docs_ui import documentation
from .openapi_presentation import DESCRIPTION, TAGS, present


def create_app() -> FastAPI:
    application = FastAPI(
        title="IACP Orchestrator", version="0.10.1",
        docs_url=None, redoc_url=None, openapi_url=None,
        servers=[{"url": "https://iac.cyberfuel.com", "description": "API oficial; acceso autorizado"}],
        description=DESCRIPTION,
        openapi_tags=TAGS,
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
            r'/v1/instances/[a-f0-9-]{36}/(?:provisioning/deletions/[a-f0-9-]{36}/(?:cancel|recover)|provisioning/executions/[a-f0-9-]{36}/(?:cancel|recover|resolve-absence)|network/discover|reconcile)', request.url.path)
        # Policy administration prepares/revokes delegation without submitting
        # infrastructure work. It must be usable before activate-lab opens gates.
        bookkeeping = bookkeeping or (request.method == 'POST' and re.fullmatch(
            r'/v1/lab/policies(?:/[a-f0-9-]{36}/(?:approve|renew|revoke))?',request.url.path))
        if re.match(r'/v1/instances/[^/]+/terraform(?:/|$)', request.url.path):
            # Removed HTTP routes stay absent even while admission is drained.
            response = JSONResponse(status_code=404, content={"detail": "Not Found"})
        elif request.method not in {"GET", "HEAD", "OPTIONS"} and not bookkeeping and os.environ.get("IACP_ADMISSION_ENABLED", "1") != "1":
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
        if request.method=='POST' and request.url.path=='/v1/instances' and response.status_code>=400:
            committed=getattr(request.state,'admission_committed',False)
            logging.getLogger('uvicorn.error').info(json.dumps({
                'event':'admission_dispatch_unconfirmed' if committed else 'admission_rejected',
                'request_id':request.state.request_id,'status':response.status_code,
                'admitted':committed,'executed':None if committed else False}))
        return response

    @application.get("/docs", include_in_schema=False, dependencies=[Depends(authorize_docs)])
    def docs(request: Request):
        return documentation(request, application.version)

    @application.get("/openapi.json", include_in_schema=False, dependencies=[Depends(authorize_docs)])
    def contract():
        return JSONResponse(application.openapi())

    @application.get("/admin/docs", include_in_schema=False, dependencies=[Depends(authorize_admin_docs)])
    def admin_docs(request: Request):
        return documentation(request, application.version, administrative=True)

    @application.get("/admin/openapi.json", include_in_schema=False, dependencies=[Depends(authorize_admin_docs)])
    def admin_contract():
        return JSONResponse(application.administrative_openapi())

    application.include_router(health.router)
    application.include_router(jobs.router)
    creation_contract=os.environ.get('IACP_CREATION_CONTRACT','historical')
    if creation_contract not in {'historical','commercial-g'}:
        raise ValueError('CREATION_CONTRACT_INVALID')
    if creation_contract=='commercial-g':
        from .api import commercial
        from fastapi import APIRouter
        historical_reads=APIRouter(routes=[r for r in instances.router.routes
            if not (r.path=='/v1/instances' and 'POST' in r.methods)])
        application.include_router(historical_reads)
        application.include_router(commercial.router)
    else:
        application.include_router(instances.router)
    application.include_router(catalog.router)
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

    from .services.provisioning import ProvisioningError

    @application.exception_handler(ProvisioningError)
    def executor_unavailable(request, error):
        from .api.presentation import public_code
        return JSONResponse(status_code=503, content={"detail": public_code(error.code)})

    from fastapi import HTTPException

    @application.exception_handler(HTTPException)
    def http_failure(request, error):
        from .api.presentation import public_code
        return JSONResponse(status_code=error.status_code,
            content={"detail": public_code(error.detail) if isinstance(error.detail, str) else error.detail},
            headers=error.headers)

    @application.exception_handler(SQLAlchemyError)
    def persistence_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "Persistence unavailable"})

    def build_contract(administrative=False):
        routes = []
        for route in application.routes:
            if not isinstance(route, APIRoute):
                continue
            admin = ("/provisioning/" in route.path or (
                route.path.startswith("/v1/lab/policies") and "POST" in route.methods))
            if administrative or not admin:
                routes.append(route)
        tags = application.openapi_tags if administrative else [tag for tag in application.openapi_tags
            if tag["name"] not in {"provisioning-administration", "provisioning-approvals", "lab-administration"}]
        return present(get_openapi(title=application.title, version=application.version,
            description=application.description, servers=application.servers, tags=tags, routes=routes))

    # Separate schemas, same handlers. Authorization is enforced by dependencies,
    # regardless of which documentation a caller has viewed.
    application.openapi = lambda: build_contract()
    application.administrative_openapi = lambda: build_contract(administrative=True)

    return application


app = create_app()
