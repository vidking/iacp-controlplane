from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .api import health, instances, jobs, operations, terraform


def create_app() -> FastAPI:
    application = FastAPI(
        title="IACP Control Plane", version="0.4.0",
        description=(
            "Inventario por tenant y operaciones asíncronas. Simulación por defecto y "
            "planificación Terraform optativa; apply permanece deshabilitado."
        ),
    )
    application.include_router(health.router)
    application.include_router(jobs.router)
    application.include_router(instances.router)
    application.include_router(operations.router)
    application.include_router(terraform.router)

    from .terraform.config import PlanError

    @application.exception_handler(PlanError)
    def executor_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": error.code})

    @application.exception_handler(SQLAlchemyError)
    def persistence_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "Persistence unavailable"})

    return application


app = create_app()
