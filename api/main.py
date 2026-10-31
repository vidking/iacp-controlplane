import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from api.db import database, schema_revision
from api.models import InstanceCreateRequest, OperationRequest, PowerAction, ProvisionRequest
from api.service import (
    create_destroy,
    create_instance,
    create_operation,
    create_power,
    create_provision,
    image_catalog,
    instance_row,
    list_instances,
    now,
    operation_row,
)
from worker.proxmox import ProxmoxClient, ProxmoxError


tags_metadata = [
    {"name": "health", "description": "Estado tecnico del API."},
    {"name": "provisioning", "description": "Crear, aplicar, observar y destruir instancias."},
    {"name": "power", "description": "Estado, inicio, apagado y reinicio de KVM."},
    {"name": "operations", "description": "Ordenes asíncronas y seguimiento."},
    {"name": "instances", "description": "Inventario técnico de instancias administradas."},
    {"name": "observability", "description": "Eventos, workers y heartbeats."},
]

app = FastAPI(
    title="IACP Control Plane Técnico",
    version="0.4.0",
    description=(
        "Orquestador técnico simple para ejecutar Terraform sobre el clúster MNG."
    ),
    openapi_tags=tags_metadata,
)

CONSOLE_DIR = Path(__file__).parent / "static" / "console"

bearer = HTTPBearer(auto_error=False, scheme_name="Bearer")


def _api_token() -> str:
    path = os.getenv("IACP_API_TOKEN_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.getenv("IACP_API_TOKEN", "").strip()


def authorize(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    configured = _api_token()
    if not configured:
        raise HTTPException(status_code=503, detail="Authentication unavailable")
    if credentials is None or credentials.credentials != configured:
        raise HTTPException(status_code=401, detail="invalid token")


@app.on_event("startup")
def startup() -> None:
    # El servicio migrate es la única autoridad de cambios de esquema.
    # API no crea tablas al arrancar ni puede adelantar una migración.
    return None


@app.get("/", include_in_schema=False, tags=["health"])
def root():
    return {
        "service": "iacp-controlplane-tecnico",
        "console": "/console",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


@app.get("/console", include_in_schema=False)
def console():
    return FileResponse(CONSOLE_DIR / "index.html")


app.mount(
    "/console/assets",
    StaticFiles(directory=CONSOLE_DIR / "assets"),
    name="console-assets",
)


@app.get("/health/live", tags=["health"])
def live():
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"])
def ready():
    if schema_revision() != "0001_technical_schema":
        raise HTTPException(status_code=503, detail="schema técnico no está migrado")
    return {"status": "ready"}


@app.get("/v1/images", dependencies=[Depends(authorize)], tags=["provisioning"])
def images():
    return {"images": image_catalog()}


@app.get("/v1/plans", dependencies=[Depends(authorize)], tags=["provisioning"])
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
    tags=["provisioning"],
    summary="Crear una instancia técnica",
    description=(
        "Crea una instancia persistida y encola su plan Terraform. "
        "Después de que el plan termine, use /provision para ejecutar apply."
    ),
)
def post_instance(request: InstanceCreateRequest):
    return create_instance(request)


@app.post(
    "/v1/instances/{instance_id}/provision",
    dependencies=[Depends(authorize)],
    tags=["provisioning"],
    summary="Aplicar el plan de una instancia",
    description="Ejecuta el apply del último plan exitoso sin volver a resolver Terraform.",
)
def provision_instance(instance_id: str, request: ProvisionRequest | None = None):
    return create_provision(instance_id, request.plan_operation_id if request else None)


@app.delete(
    "/v1/instances/{instance_id}",
    dependencies=[Depends(authorize)],
    tags=["provisioning"],
    summary="Destruir una instancia",
    description="Encola destroy usando el workspace y state del plan original.",
)
def delete_instance(instance_id: str):
    return create_destroy(instance_id)


@app.get("/v1/instances", dependencies=[Depends(authorize)], tags=["instances"])
def list_instance_endpoint(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, max_length=32),
    name: str | None = Query(default=None, max_length=63),
    include_deleted: bool = Query(default=False),
):
    return list_instances(
        limit=limit,
        offset=offset,
        status=status,
        name=name,
        include_deleted=include_deleted,
    )


def _power_target(instance_id: str) -> dict:
    with database() as db:
        row = db.execute(
            "SELECT instance_id, name, vmid, node, status, power_state FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    if row["vmid"] is None or row["status"] == "deleted":
        raise HTTPException(status_code=409, detail="la instancia todavía no tiene una VM administrada")
    return dict(row)


@app.get(
    "/v1/instances/{instance_id}/power",
    dependencies=[Depends(authorize)],
    tags=["power"],
    summary="Consultar el estado de energía",
    description=(
        "Consulta directamente Proxmox. Running o stopped describe energía, "
        "no disponibilidad del sistema operativo."
    ),
)
def get_power(instance_id: str):
    target = _power_target(instance_id)
    try:
        client = ProxmoxClient(readonly=True)
        client.permissions(target["node"], int(target["vmid"]), mutate=False)
        observed = client.power_state(target["node"], int(target["vmid"]), target["name"])
        mutations_available = True
        blocked_reason = None
        try:
            client.permissions(target["node"], int(target["vmid"]), mutate=True)
        except ProxmoxError as error:
            if str(error) == "POWER_PERMISSION_DENIED":
                mutations_available = False
                blocked_reason = str(error)
            else:
                raise
        with database() as db:
            db.execute(
                "UPDATE instances SET power_state=?, updated_at=? WHERE instance_id=?",
                (observed["power_state"], now(), instance_id),
            )
        return {
            "instance_id": instance_id,
            **observed,
            "mutations_available": mutations_available,
            "blocked_reason": blocked_reason,
        }
    except ProxmoxError as error:
        code = str(error)
        status_code = 403 if code == "POWER_PERMISSION_DENIED" else 503
        raise HTTPException(status_code=status_code, detail=code) from error


@app.post(
    "/v1/instances/{instance_id}/power/start",
    dependencies=[Depends(authorize)],
    tags=["power"],
    status_code=202,
    summary="Encender una instancia",
    description="Encola start. Si el token no tiene VM.PowerMgmt, la operación termina con POWER_PERMISSION_DENIED.",
)
def start_instance(instance_id: str):
    return create_power(instance_id, PowerAction.start)


@app.post(
    "/v1/instances/{instance_id}/power/shutdown",
    dependencies=[Depends(authorize)],
    tags=["power"],
    status_code=202,
    summary="Apagar una instancia",
    description="Encola un apagado ACPI sin fallback forzado.",
)
def shutdown_instance(instance_id: str):
    return create_power(instance_id, PowerAction.shutdown)


@app.post(
    "/v1/instances/{instance_id}/power/reboot",
    dependencies=[Depends(authorize)],
    tags=["power"],
    status_code=202,
    summary="Reiniciar una instancia",
    description="Encola reboot y requiere que la VM esté running.",
)
def reboot_instance(instance_id: str):
    return create_power(instance_id, PowerAction.reboot)


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
            "SELECT COUNT(*) AS count FROM operations WHERE status IN ('pending', 'planning', 'planned', 'applying', 'running', 'destroying', 'powering')"
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
