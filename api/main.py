import os
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.db import database, initialize
from api.models import OperationRequest
from api.service import create_operation, image_catalog, instance_row, operation_row


tags_metadata = [
    {"name": "health", "description": "Estado tecnico del API."},
    {"name": "operations", "description": "Ordenes plan, apply y destroy."},
    {"name": "instances", "description": "Estado de instancias administradas."},
    {"name": "observability", "description": "Eventos, workers y heartbeats."},
]

app = FastAPI(
    title="IACP Control Plane V2",
    version="0.3.0",
    description=(
        "Orquestador tecnico simple para ejecutar Terraform sobre el cluster MNG. "
        "No consulta CRM, ownership ni permisos comerciales."
    ),
    openapi_tags=tags_metadata,
)

bearer = HTTPBearer(auto_error=False, scheme_name="Bearer")


def authorize(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    configured = os.getenv("IACP_API_TOKEN", "").strip()
    if configured and (credentials is None or credentials.credentials != configured):
        raise HTTPException(status_code=401, detail="invalid token")


@app.on_event("startup")
def startup() -> None:
    initialize()


@app.get("/", include_in_schema=False, tags=["health"])
def root():
    return {"service": "iacp-controlplanev2", "docs": "/docs", "openapi": "/openapi.json"}


@app.get("/health/live", tags=["health"])
def live():
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"])
def ready():
    initialize()
    return {"status": "ready"}


@app.get("/v1/images", dependencies=[Depends(authorize)], tags=["operations"])
def images():
    return {"images": image_catalog()}


@app.get("/v1/plans", dependencies=[Depends(authorize)], tags=["operations"])
def plans():
    return {
        "plans": [
            {"plan_id": "kvm-1", "vcpus": 1, "memory_mb": 2048, "disk_gb": 20},
            {"plan_id": "kvm-2", "vcpus": 2, "memory_mb": 4096, "disk_gb": 40},
            {"plan_id": "kvm-3", "vcpus": 4, "memory_mb": 8192, "disk_gb": 80},
        ]
    }


@app.post(
    "/v1/operations",
    dependencies=[Depends(authorize)],
    tags=["operations"],
    summary="Encolar una orden Terraform",
    description="Acepta una orden plan, apply o destroy y devuelve sus identificadores.",
)
def post_operation(request: OperationRequest):
    return create_operation(request)


@app.post(
    "/v1/instances",
    dependencies=[Depends(authorize)],
    tags=["instances"],
    summary="Crear un plan de instancia",
)
def post_instance(request: OperationRequest):
    if request.action.value != "provision":
        raise HTTPException(status_code=422, detail="/v1/instances solo admite provision")
    return create_operation(request)


@app.get("/v1/operations", dependencies=[Depends(authorize)], tags=["operations"])
def list_operations(limit: int = Query(default=50, ge=1, le=200)):
    with database() as db:
        rows = db.execute("SELECT * FROM operations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"operations": [operation_row(row) for row in rows]}


@app.get("/v1/operations/{operation_id}", dependencies=[Depends(authorize)], tags=["operations"])
def get_operation(operation_id: str):
    with database() as db:
        row = db.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    return operation_row(row)


@app.get("/v1/instances/{instance_id}", dependencies=[Depends(authorize)], tags=["instances"])
def get_instance(instance_id: str):
    with database() as db:
        row = db.execute("SELECT * FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
    return instance_row(row)


@app.get("/v1/events", dependencies=[Depends(authorize)], tags=["observability"])
def list_events(after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
    with database() as db:
        rows = db.execute(
            "SELECT * FROM events WHERE event_id > ? ORDER BY event_id LIMIT ?", (after, limit)
        ).fetchall()
    return {"events": [dict(row) for row in rows], "next_after": rows[-1]["event_id"] if rows else after}


@app.get("/v1/workers", dependencies=[Depends(authorize)], tags=["observability"])
def list_workers():
    with database() as db:
        rows = db.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
    return {"workers": [dict(row) for row in rows]}


@app.get("/v1/observability", dependencies=[Depends(authorize)], tags=["observability"])
def observability():
    with database() as db:
        workers = [dict(row) for row in db.execute("SELECT * FROM workers ORDER BY worker_id")]
        active = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE status IN ('pending', 'planning', 'planned', 'applying', 'running', 'destroying')"
        ).fetchone()["count"]
        uncertain = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE status = 'uncertain'"
        ).fetchone()["count"]
        instances = db.execute(
            "SELECT COUNT(*) AS count FROM instances WHERE status NOT IN ('deleted', 'failed')"
        ).fetchone()["count"]
    return {
        "worker_liveness": "known" if workers else "unknown",
        "workers": workers,
        "active_operations": active,
        "uncertain_operations": uncertain,
        "managed_instances": instances,
    }

