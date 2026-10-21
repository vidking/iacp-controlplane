from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .api import health, instances, jobs, operations


def create_app() -> FastAPI:
    application = FastAPI(
        title="IACP Control Plane", version="0.3.0",
        description=(
            "Inventario por tenant y operaciones asíncronas con ejecutor simulado. "
            "Terraform y el aprovisionamiento real permanecen deshabilitados."
        ),
    )
    application.include_router(health.router)
    application.include_router(jobs.router)
    application.include_router(instances.router)
    application.include_router(operations.router)

    @application.exception_handler(SQLAlchemyError)
    def persistence_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "Persistence unavailable"})

    return application


app = create_app()
