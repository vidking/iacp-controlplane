from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .api import health, instances, jobs, operations, terraform, applies, power


def create_app() -> FastAPI:
    application = FastAPI(
        title="IACP Control Plane", version="0.5.0",
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
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

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
