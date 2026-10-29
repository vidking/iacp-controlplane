import json
import os
import uuid
from datetime import datetime, timezone
from enum import Enum

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from api.db import database, initialize


app = FastAPI(title="IACP Control Plane V2", version="0.1.0")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def authorize(authorization: str | None = Header(default=None)) -> None:
    configured = os.getenv("IACP_API_TOKEN", "").strip()
    if configured and authorization != f"Bearer {configured}":
        raise HTTPException(status_code=401, detail="invalid token")


class Command(str, Enum):
    plan = "plan"
    apply = "apply"
    destroy = "destroy"


class OperationRequest(BaseModel):
    command: Command
    variables: dict[str, object] = Field(default_factory=dict)
    plan_operation_id: str | None = None


@app.on_event("startup")
def startup() -> None:
    initialize()


@app.get("/health/live")
def live():
    return {"status": "alive"}


@app.get("/health/ready")
def ready():
    initialize()
    return {"status": "ready"}


@app.post("/v1/operations", dependencies=[Depends(authorize)])
def create_operation(request: OperationRequest):
    operation_id = str(uuid.uuid4())
    created = now()
    payload = request.model_dump(mode="json")
    with database() as db:
        db.execute(
            "INSERT INTO operations(operation_id, command, status, payload_json, created_at) VALUES (?, ?, 'pending', ?, ?)",
            (operation_id, request.command.value, json.dumps(payload), created),
        )
        db.execute(
            "INSERT INTO events(operation_id, kind, status, occurred_at) VALUES (?, 'operation.created', 'pending', ?)",
            (operation_id, created),
        )
    return {"operation_id": operation_id, "status": "pending", "command": request.command.value}


def operation_row(row):
    if row is None:
        raise HTTPException(status_code=404, detail="operation not found")
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


@app.get("/v1/operations", dependencies=[Depends(authorize)])
def list_operations(limit: int = Query(default=50, ge=1, le=200)):
    with database() as db:
        rows = db.execute("SELECT * FROM operations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"operations": [operation_row(row) for row in rows]}


@app.get("/v1/operations/{operation_id}", dependencies=[Depends(authorize)])
def get_operation(operation_id: str):
    with database() as db:
        row = db.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    return operation_row(row)


@app.get("/v1/events", dependencies=[Depends(authorize)])
def list_events(after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
    with database() as db:
        rows = db.execute(
            "SELECT * FROM events WHERE event_id > ? ORDER BY event_id LIMIT ?", (after, limit)
        ).fetchall()
    return {"events": [dict(row) for row in rows], "next_after": rows[-1]["event_id"] if rows else after}


@app.get("/v1/workers", dependencies=[Depends(authorize)])
def list_workers():
    with database() as db:
        rows = db.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
    return {"workers": [dict(row) for row in rows]}


@app.get("/v1/observability", dependencies=[Depends(authorize)])
def observability():
    with database() as db:
        workers = [dict(row) for row in db.execute("SELECT * FROM workers ORDER BY worker_id")]
        active = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE status IN ('pending', 'running')"
        ).fetchone()["count"]
        uncertain = db.execute(
            "SELECT COUNT(*) AS count FROM operations WHERE status = 'uncertain'"
        ).fetchone()["count"]
    return {
        "worker_liveness": "known" if workers else "unknown",
        "workers": workers,
        "active_operations": active,
        "uncertain_operations": uncertain,
    }

