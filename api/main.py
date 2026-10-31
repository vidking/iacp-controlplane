import os
import json
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from api.db import database, initialize, prune_history
from api.catalog import network_profile_catalog
from api.models import InstanceCreateRequest, OperationRequest, PowerAction, ProvisionRequest
from api.service import (
    PUBLIC_OPERATION_STATUS,
    create_destroy,
    create_instance,
    create_operation,
    create_power,
    create_provision,
    create_reconcile,
    event_row,
    image_catalog,
    instance_row,
    latest_event,
    list_instances,
    list_operations as list_operation_rows,
    now,
    operation_row,
    operation_timings,
    plan_catalog_response,
    public_error_code,
    public_instance_status,
    public_operation_error,
    public_operation_output,
)
from worker.proxmox import ProxmoxClient, ProxmoxError


tags_metadata = [
    {"name": "instances", "description": "Perfiles, creación y consulta de instancias propias del tenant."},
    {"name": "operations", "description": "Seguimiento de intenciones asíncronas, estados terminales y eventos."},
    {"name": "kvm-lifecycle", "description": "Estado de energía, inicio, reinicio y apagado ordenado de KVM autorizadas."},
    {"name": "disposable-deletion", "description": "Eliminación autorizada de instancias descartables; los recursos protegidos quedan excluidos."},
    {"name": "observability", "description": "Diagnóstico, eventos, workers y estado del tenant; no sustituye la supervisión de infraestructura."},
    {"name": "provisioning", "description": "Catálogo técnico, plan/apply explícito y compatibilidad operativa."},
    {"name": "health", "description": "Estado técnico del API."},
]

app = FastAPI(
    title="IACP Control Plane V2",
    version="1.0.0",
    description=(
        "Orquestador técnico para administrar instancias KVM mediante un contrato estable. "
        "Los metadatos comerciales son trazabilidad; no conceden permisos ni cambian el plan de infraestructura."
    ),
    openapi_tags=tags_metadata,
    docs_url=None,
    redoc_url=None,
)

CONSOLE_DIR = Path(__file__).parent / "static" / "console"
DOCS_DIR = Path(__file__).parent / "static" / "docs"

bearer = HTTPBearer(auto_error=False, scheme_name="Bearer")


def _require_api_token() -> bool:
    return os.getenv("IACP_REQUIRE_API_TOKEN", "0").strip().lower() not in {"0", "false", "no", "off"}


def _max_concurrent() -> int:
    try:
        return max(1, int(os.getenv("IACP_MAX_CONCURRENT", "2")))
    except ValueError:
        return 2


def _heartbeat_age(value: str | None) -> float | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())


def _elapsed_seconds(start: str | None, end: str | None = None) -> float | None:
    if not start:
        return None
    try:
        started = datetime.fromisoformat(start)
        finished = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return round(max(0.0, (finished - started).total_seconds()), 3)


def _worker_stale_seconds() -> float:
    return max(30.0, float(os.getenv("IACP_WORKER_STALE_SECONDS", "900")))


def _worker_liveness(worker: dict) -> str:
    age = worker.get("heartbeat_age_seconds")
    if age is None:
        return "unknown"
    return "healthy" if age <= _worker_stale_seconds() else "stale"


def authorize(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    configured = os.getenv("IACP_API_TOKEN", "").strip()
    if _require_api_token() and not configured:
        raise HTTPException(status_code=503, detail="API token no configurado")
    if configured and (credentials is None or credentials.credentials != configured):
        raise HTTPException(status_code=401, detail="invalid token")


@app.on_event("startup")
def startup() -> None:
    initialize()
    prune_history()
    if _require_api_token() and not os.getenv("IACP_API_TOKEN", "").strip():
        raise RuntimeError("IACP_API_TOKEN es obligatorio cuando IACP_REQUIRE_API_TOKEN está activo")


@app.get("/", include_in_schema=False, tags=["health"])
def root():
    return {
        "service": "iacp-controlplanev2",
        "console": "/docs#console",
        "docs": "/docs",
        "swagger": "/swagger",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }


@app.get("/docs", include_in_schema=False)
def docs_portal():
    return FileResponse(DOCS_DIR / "index.html")


@app.get("/swagger", include_in_schema=False)
def swagger():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title="IACP Orchestrator · Swagger",
        swagger_favicon_url="/docs/assets/favicon.svg",
    )


@app.get("/redoc", include_in_schema=False)
def redoc():
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title="IACP Orchestrator · ReDoc",
        redoc_favicon_url="/docs/assets/favicon.svg",
    )


@app.get("/console", include_in_schema=False)
def console():
    return RedirectResponse(url="/docs#console", status_code=307)


app.mount(
    "/console/assets",
    StaticFiles(directory=CONSOLE_DIR / "assets"),
    name="console-assets",
)

app.mount(
    "/docs/assets",
    StaticFiles(directory=DOCS_DIR / "assets"),
    name="docs-assets",
)


@app.get("/health/live", tags=["health"])
def live():
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"])
def ready():
    initialize()
    return {"status": "ready"}


@app.get(
    "/v1/images",
    dependencies=[Depends(authorize)],
    tags=["instances"],
    summary="Consultar Golden Images y su autorización",
    operation_id="listImages",
)
def images():
    return {"images": image_catalog()}


@app.get(
    "/v1/plans",
    dependencies=[Depends(authorize)],
    tags=["instances"],
    summary="Consultar los planes certificados",
    operation_id="listPlans",
)
def plans():
    return {"plans": plan_catalog_response()}


@app.get(
    "/v1/network-profiles",
    dependencies=[Depends(authorize)],
    tags=["instances"],
    summary="Consultar perfiles de red disponibles",
    description=(
        "Devuelve los perfiles de red certificados. El consumidor elige el perfil, "
        "pero no envía el prefijo, la IP ni el gateway."
    ),
    operation_id="listNetworkProfiles",
)
def network_profiles():
    return {"network_profiles": network_profile_catalog()}


@app.post(
    "/v1/operations",
    dependencies=[Depends(authorize)],
    tags=["operations"],
    summary="Encolar una orden de orquestación",
    description="Acepta una orden de ciclo de vida y devuelve sus identificadores para seguimiento.",
    operation_id="enqueueOperation",
)
def post_operation(request: OperationRequest):
    return create_operation(request)


@app.post(
    "/v1/instances",
    dependencies=[Depends(authorize)],
    tags=["instances"],
    status_code=202,
    summary="Crear una instancia técnica",
    description=(
        "Crea una instancia persistida y encola el plan y apply como una operación transparente. "
        "Use Idempotency-Key para reintentos seguros."
    ),
    operation_id="createInstance",
)
def post_instance(
    request: InstanceCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return create_instance(request, idempotency_key=idempotency_key)


@app.post(
    "/v1/instances/{instance_id}/provision",
    dependencies=[Depends(authorize)],
    tags=["provisioning"],
    summary="Aplicar el plan de una instancia",
    description="Continúa el aprovisionamiento de una instancia usando la configuración validada.",
    operation_id="provisionInstance",
)
def provision_instance(instance_id: str, request: ProvisionRequest | None = None):
    return create_provision(instance_id, request.plan_operation_id if request else None)


@app.delete(
    "/v1/instances/{instance_id}",
    dependencies=[Depends(authorize)],
    tags=["disposable-deletion"],
    status_code=202,
    summary="Destruir una instancia",
    description="Encola destroy usando el workspace y state del plan original.",
    operation_id="deleteInstance",
)
def delete_instance(
    instance_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return create_destroy(instance_id, idempotency_key=idempotency_key)


@app.post(
    "/v1/instances/{instance_id}/reconcile",
    dependencies=[Depends(authorize)],
    tags=["operations"],
    status_code=202,
    summary="Reconciliar una operación incierta",
    description=(
        "Vuelve a observar el estado físico y recupera de forma segura la última operación uncertain. "
        "No reinicia la instancia automáticamente."
    ),
    operation_id="reconcileInstance",
)
def reconcile_instance(
    instance_id: str,
    source_operation_id: str | None = Query(default=None, max_length=64),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return create_reconcile(
        instance_id,
        source_operation_id=source_operation_id,
        idempotency_key=idempotency_key,
    )


@app.get(
    "/v1/instances",
    dependencies=[Depends(authorize)],
    tags=["instances"],
    summary="Consultar las instancias del tenant",
    description="Lista instancias administradas con filtros opcionales, paginación y estado público normalizado.",
    operation_id="listInstances",
)
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
    tags=["kvm-lifecycle"],
    summary="Consultar el estado de energía",
    description=(
        "Consulta directamente Proxmox. Running o stopped describe energía, "
        "no disponibilidad del sistema operativo."
    ),
    operation_id="getInstancePower",
)
def get_power(instance_id: str):
    target = _power_target(instance_id)
    try:
        client = ProxmoxClient()
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


def _guest_ipv4(interfaces: list[dict]) -> list[str]:
    addresses: list[str] = []
    for interface in interfaces:
        entries = interface.get("ip-addresses") or interface.get("ip_addresses") or []
        for entry in entries:
            address = entry.get("ip-address") or entry.get("ip_address")
            if not address:
                continue
            try:
                parsed = ip_address(str(address))
            except ValueError:
                continue
            if parsed.version == 4 and not (
                parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified
            ):
                addresses.append(str(parsed))
    return list(dict.fromkeys(addresses))


@app.get(
    "/v1/instances/{instance_id}/network",
    dependencies=[Depends(authorize)],
    tags=["instances"],
    summary="Observar las IP de una instancia",
    description="Consulta las interfaces reportadas por el QEMU Guest Agent; no confunde configuración con conectividad.",
    operation_id="getInstanceNetwork",
)
def get_network(instance_id: str):
    with database() as db:
        row = db.execute(
            "SELECT instance_id, vmid, node, status, spec_json FROM instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    spec = json.loads(row["spec_json"] or "{}")
    response = {
        "instance_id": instance_id,
        "vmid": row["vmid"],
        "node": row["node"],
        "network_profile": spec.get("network_profile", "lab_dhcp"),
        "configured_ipv4_cidr": spec.get("ipv4_cidr"),
        "configured_ipv4_gateway": spec.get("ipv4_gateway"),
        "observed_ipv4": [],
        "source": "none",
        "status": "pending" if row["vmid"] is None else "unavailable",
        "reason": "VM_NOT_PROVISIONED" if row["vmid"] is None else None,
        "observed_at": now(),
    }
    with database() as db:
        allocation = db.execute(
            """
            SELECT provider, profile, address, gateway, status, created_at, released_at
            FROM ip_allocations
            WHERE instance_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
    response["network_allocation"] = dict(allocation) if allocation is not None else None
    if response["network_allocation"] is None and spec.get("network_profile") == "prod_static":
        response["network_allocation"] = {
            "provider": "netbox",
            "profile": "prod_static",
            "address": spec.get("ipv4_cidr"),
            "gateway": spec.get("ipv4_gateway"),
            "status": "pending",
        }
    if row["vmid"] is None or row["status"] == "deleted":
        return response
    try:
        interfaces = ProxmoxClient().guest_agent_interfaces(row["node"], int(row["vmid"]))
    except ProxmoxError as error:
        response["reason"] = str(error)
        return response
    addresses = _guest_ipv4(interfaces)
    response["observed_ipv4"] = addresses
    response["source"] = "guest_agent"
    response["status"] = "observed" if addresses else "unavailable"
    response["reason"] = None if addresses else "GUEST_AGENT_NO_IPV4"
    return response


@app.post(
    "/v1/instances/{instance_id}/power/start",
    dependencies=[Depends(authorize)],
    tags=["kvm-lifecycle"],
    status_code=202,
    summary="Encender una instancia",
    description="Encola start y devuelve un operation_id para seguir el cambio de energía.",
    operation_id="startInstance",
)
def start_instance(
    instance_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return create_power(instance_id, PowerAction.start, idempotency_key=idempotency_key)


@app.post(
    "/v1/instances/{instance_id}/power/shutdown",
    dependencies=[Depends(authorize)],
    tags=["kvm-lifecycle"],
    status_code=202,
    summary="Apagar una instancia",
    description="Encola un apagado ordenado y usa stop seguro si Proxmox reporta timeout de ACPI/Guest Agent.",
    operation_id="shutdownInstance",
)
def shutdown_instance(
    instance_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return create_power(instance_id, PowerAction.shutdown, idempotency_key=idempotency_key)


@app.post(
    "/v1/instances/{instance_id}/power/reboot",
    dependencies=[Depends(authorize)],
    tags=["kvm-lifecycle"],
    status_code=202,
    summary="Reiniciar una instancia",
    description="Encola reboot y requiere que la VM esté running.",
    operation_id="rebootInstance",
)
def reboot_instance(
    instance_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return create_power(instance_id, PowerAction.reboot, idempotency_key=idempotency_key)


@app.get(
    "/v1/operations",
    dependencies=[Depends(authorize)],
    tags=["operations"],
    summary="Consultar el historial de operaciones",
    description="Lista ejecuciones asíncronas con filtros por instancia y estado.",
    operation_id="listOperations",
)
def list_operations(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    instance_id: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=32),
):
    return list_operation_rows(
        limit=limit,
        offset=offset,
        instance_id=instance_id,
        status=status,
    )


@app.get(
    "/v1/operations/{operation_id}",
    dependencies=[Depends(authorize)],
    tags=["operations"],
    summary="Seguir una operación por su UUID",
    operation_id="getOperation",
)
def get_operation(operation_id: str):
    with database() as db:
        row = db.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    return operation_row(row)


@app.get(
    "/v1/instances/{instance_id}",
    dependencies=[Depends(authorize)],
    tags=["instances"],
    summary="Consultar una instancia",
    operation_id="getInstance",
)
def get_instance(instance_id: str):
    with database() as db:
        row = db.execute("SELECT * FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
    return instance_row(row)


@app.get(
    "/v1/events",
    dependencies=[Depends(authorize)],
    tags=["observability"],
    summary="Consultar eventos técnicos",
    operation_id="listEvents",
)
def list_events(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort: Literal["asc", "desc"] = Query(default="desc"),
    operation_id: str | None = Query(default=None, max_length=64),
):
    with database() as db:
        filters = ["e.event_id > ?"]
        values: list[object] = [after]
        if operation_id:
            filters.append("e.operation_id = ?")
            values.append(operation_id)
        order = "DESC" if sort == "desc" else "ASC"
        where = " AND ".join(filters)
        total = db.execute(
            f"SELECT COUNT(*) AS count FROM events e WHERE {where}", values
        ).fetchone()["count"]
        rows = db.execute(
            f"""
            SELECT e.*, o.command, o.action, o.instance_id, o.worker_id,
                   i.name AS instance_name
            FROM events e
            LEFT JOIN operations o ON o.operation_id = e.operation_id
            LEFT JOIN instances i ON i.instance_id = o.instance_id
            WHERE {where}
            ORDER BY e.event_id {order}
            LIMIT ? OFFSET ?
            """,
            (*values, limit, offset),
        ).fetchall()
        events = [event_row(db, row) for row in rows]
    return {
        "events": events,
        "next_after": max((row["event_id"] for row in rows), default=after),
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "operation_id": operation_id,
    }


def _operation_summary(row, db=None):
    if row is None:
        return None
    result = dict(row)
    raw_status = result.get("status")
    result["status"] = PUBLIC_OPERATION_STATUS.get(raw_status, raw_status)
    result["internal_status"] = raw_status
    result["job_id"] = result["operation_id"]
    result["timings"] = operation_timings(result)
    result["queue_wait_seconds"] = result["timings"]["queue_seconds"]
    result["execution_seconds"] = result["timings"]["execution_seconds"]
    result["heartbeat_age_seconds"] = _heartbeat_age(result.get("last_heartbeat"))
    result["heartbeat_stale"] = (
        result["heartbeat_age_seconds"] is not None
        and result["heartbeat_age_seconds"] > _worker_stale_seconds()
        and raw_status in {"running", "planning", "applying", "destroying", "powering"}
    )
    result["output_tail"] = public_operation_output(result)
    result["error_code"] = public_error_code(result.get("error"), raw_status)
    result["error"] = public_operation_error({**result, "status": raw_status})
    for key in ("payload_json", "secret_path", "commercial_context_json", "output", "workspace_path", "plan_path"):
        result.pop(key, None)
    result["latest_event"] = latest_event(db, result["operation_id"]) if db is not None else None
    return result


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def _timing_stats(values: list[float]) -> dict:
    return {
        "count": len(values),
        "p50_seconds": _percentile(values, 0.50),
        "p95_seconds": _percentile(values, 0.95),
    }


def _performance_summary(rows) -> dict:
    by_command: dict[str, dict[str, list[float]]] = {}
    all_timings = {"queue": [], "execution": [], "total": []}
    created_times = []
    finished_times = []
    for row in rows:
        timings = operation_timings(dict(row))
        if timings["total_seconds"] is None:
            continue
        try:
            created_times.append(datetime.fromisoformat(row["created_at"]))
            finished_times.append(datetime.fromisoformat(row["finished_at"]))
        except (TypeError, ValueError):
            pass
        command = str(row["command"])
        bucket = by_command.setdefault(command, {"queue": [], "execution": [], "total": []})
        for name, key in (("queue", "queue_seconds"), ("execution", "execution_seconds"), ("total", "total_seconds")):
            value = timings[key]
            if value is not None:
                bucket[name].append(value)
                all_timings[name].append(value)

    def summarize(values: dict[str, list[float]]) -> dict:
        return {
            "count": len(values["total"]),
            "queue": _timing_stats(values["queue"]),
            "execution": _timing_stats(values["execution"]),
            "total": _timing_stats(values["total"]),
        }

    span_seconds = None
    if created_times and finished_times:
        span_seconds = max(finished_times).timestamp() - min(created_times).timestamp()
    throughput = (
        round(len(all_timings["total"]) / span_seconds * 60, 3)
        if span_seconds and span_seconds > 0
        else None
    )
    return {
        "window_operations": 100,
        "sample_size": len(all_timings["total"]),
        "throughput_operations_per_minute": throughput,
        "queue": _timing_stats(all_timings["queue"]),
        "execution": _timing_stats(all_timings["execution"]),
        "total": _timing_stats(all_timings["total"]),
        "by_command": {command: summarize(values) for command, values in sorted(by_command.items())},
    }


def _worker_summary(db, row):
    worker = dict(row)
    worker["heartbeat_age_seconds"] = _heartbeat_age(worker.get("last_heartbeat"))
    worker["liveness"] = _worker_liveness(worker)
    worker["heartbeat_stale"] = worker["liveness"] == "stale"
    current = None
    if worker.get("current_operation_id"):
        current = db.execute(
            "SELECT o.*, i.name AS instance_name FROM operations o LEFT JOIN instances i ON i.instance_id=o.instance_id WHERE o.operation_id=?",
            (worker["current_operation_id"],),
        ).fetchone()
    last = db.execute(
        "SELECT o.*, i.name AS instance_name FROM operations o LEFT JOIN instances i ON i.instance_id=o.instance_id WHERE o.worker_id=? ORDER BY o.created_at DESC LIMIT 1",
        (worker["worker_id"],),
    ).fetchone()
    worker["current_operation"] = _operation_summary(current, db)
    worker["last_operation"] = _operation_summary(last, db)
    return worker


def _public_instance_status_counts(rows):
    counts = {}
    for row in rows:
        status = public_instance_status(row)
        counts[status] = counts.get(status, 0) + 1
    return counts


@app.get(
    "/v1/workers",
    dependencies=[Depends(authorize)],
    tags=["observability"],
    summary="Consultar workers y heartbeats",
    operation_id="listWorkers",
)
def list_workers():
    with database() as db:
        rows = db.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
        workers = [_worker_summary(db, row) for row in rows]
    return {"workers": workers}


@app.get(
    "/v1/observability",
    dependencies=[Depends(authorize)],
    tags=["observability"],
    summary="Consultar el estado de observabilidad",
    operation_id="getObservability",
)
def observability():
    with database() as db:
        workers = [_worker_summary(db, row) for row in db.execute("SELECT * FROM workers ORDER BY worker_id")]
        active = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE status IN ('pending', 'planning', 'planned', 'applying', 'running', 'destroying', 'powering')"
        ).fetchone()["count"]
        uncertain = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE status = 'uncertain'"
        ).fetchone()["count"]
        instances = db.execute(
            "SELECT COUNT(*) AS count FROM instances WHERE status NOT IN ('deleted', 'failed')"
        ).fetchone()["count"]
        instance_rows = db.execute("SELECT status, power_state FROM instances").fetchall()
        operation_rows = db.execute("SELECT status, COUNT(*) AS count FROM operations GROUP BY status").fetchall()
        recent_operations = [
            _operation_summary(row, db)
            for row in db.execute("SELECT * FROM operations ORDER BY created_at DESC LIMIT 20").fetchall()
        ]
        recent_events = [
            event_row(db, row)
            for row in db.execute(
                """
                SELECT e.*, o.command, o.action, o.instance_id, o.worker_id,
                       i.name AS instance_name
                FROM events e
                LEFT JOIN operations o ON o.operation_id = e.operation_id
                LEFT JOIN instances i ON i.instance_id = o.instance_id
                ORDER BY e.event_id DESC
                LIMIT 20
                """
            ).fetchall()
        ]
        events_total = db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
        queue_depth = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE status='pending'"
        ).fetchone()["count"]
        performance_rows = db.execute(
            """
            SELECT command, created_at, started_at, finished_at
            FROM operations
            WHERE status='succeeded' AND finished_at IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT 100
            """
        ).fetchall()
        performance = _performance_summary(performance_rows)
    liveness = [worker.get("liveness") for worker in workers]
    if not liveness:
        worker_liveness = "unknown"
    elif all(value == "healthy" for value in liveness):
        worker_liveness = "healthy"
    elif any(value == "healthy" for value in liveness):
        worker_liveness = "degraded"
    elif any(value == "unknown" for value in liveness):
        worker_liveness = "unknown"
    else:
        worker_liveness = "stale"
    return {
        "worker_liveness": worker_liveness,
        "worker_stale_after_seconds": _worker_stale_seconds(),
        "workers": workers,
        "max_concurrent": _max_concurrent(),
        "active_operations": active,
        "queue_depth": queue_depth,
        "uncertain_operations": uncertain,
        "performance": performance,
        "managed_instances": instances,
        "instance_status_counts": _public_instance_status_counts(instance_rows),
        "operation_status_counts": {
            PUBLIC_OPERATION_STATUS.get(row["status"], row["status"]): row["count"] for row in operation_rows
        },
        "recent_operations": recent_operations,
        "recent_events": recent_events,
        "events_total": events_total,
    }

