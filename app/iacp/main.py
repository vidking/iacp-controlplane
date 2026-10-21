from fastapi import FastAPI

from .api import health, instances, jobs


def create_app() -> FastAPI:
    application = FastAPI(
        title="IACP Control Plane", version="0.2.0",
        description=(
            "Control Plane en fase de ingeniería. Token de laboratorio compartido; "
            "aislamiento por tenant y aprovisionamiento pendientes."
        ),
    )
    application.include_router(health.router)
    application.include_router(jobs.router)
    application.include_router(instances.router)
    return application


app = create_app()
