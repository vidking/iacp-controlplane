import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from api.db import database
from api.models import Action, Command, OperationRequest


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(db, operation_id: str, kind: str, status: str, detail: str = "") -> None:
    db.execute(
        "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, ?, ?, ?, ?)",
        (operation_id, kind, status, now(), detail),
    )


def create_operation(request: OperationRequest) -> dict:
    operation_id = str(uuid.uuid4())
    instance_id = str(uuid.uuid4()) if request.command == Command.plan else request.instance_id
    effective_stack = request.stack.value if hasattr(request.stack, "value") else request.stack
    created = now()
    payload = request.model_dump(mode="json")
    with database() as db:
        if request.plan_operation_id:
            source = db.execute(
                "SELECT instance_id, stack FROM operations WHERE operation_id = ?",
                (request.plan_operation_id,),
            ).fetchone()
            if source is None:
                raise HTTPException(status_code=404, detail="plan_operation_id no existe")
            instance_id = instance_id or source["instance_id"]
            if source["stack"]:
                effective_stack = source["stack"]
                payload["stack"] = effective_stack
        if request.command in {Command.apply, Command.destroy} and not instance_id:
            raise HTTPException(status_code=422, detail="la operacion no tiene instancia objetivo")
        payload["instance_id"] = instance_id
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json, instance_id,
              plan_operation_id, stack, created_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                request.command.value,
                request.action.value,
                json.dumps(payload),
                instance_id,
                request.plan_operation_id,
                effective_stack,
                created,
            ),
        )
        if request.command == Command.plan and request.action == Action.provision and request.spec is not None:
            db.execute(
                """
                INSERT INTO instances(instance_id, operation_id, name, image, status, spec_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    instance_id,
                    operation_id,
                    request.spec.name,
                    request.spec.image,
                    request.spec.model_dump_json(),
                    created,
                    created,
                ),
            )
        emit(db, operation_id, "operation.created", "pending")
    return {
        "operation_id": operation_id,
        "instance_id": instance_id,
        "status": "pending",
        "command": request.command.value,
        "action": request.action.value,
    }


def operation_row(row):
    if row is None:
        raise HTTPException(status_code=404, detail="operation not found")
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


def instance_row(row):
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    result = dict(row)
    result["spec"] = json.loads(result.pop("spec_json"))
    return result


def image_catalog() -> list[dict]:
    path = os.getenv("IACP_MNG_CATALOG", "/workspace/terraform/environments/mng01/catalog.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["images"]
