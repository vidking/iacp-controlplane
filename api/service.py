import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from api.db import database
from api.models import Action, Command, InstanceCreateRequest, OperationRequest, PowerAction
from api.secrets import encrypt_secret


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(db, operation_id: str, kind: str, status: str, detail: str = "") -> None:
    db.execute(
        "INSERT INTO events(operation_id, kind, status, occurred_at, detail) VALUES (?, ?, ?, ?, ?)",
        (operation_id, kind, status, now(), detail),
    )


ACTIVE_OPERATION_STATUSES = {
    "pending",
    "running",
    "planning",
    "planned",
    "applying",
    "destroying",
    "powering",
}


def _active_instance_operation(db, instance_id: str):
    placeholders = ",".join("?" for _ in ACTIVE_OPERATION_STATUSES)
    return db.execute(
        f"""
        SELECT operation_id, command, action, status
        FROM operations
        WHERE instance_id = ? AND status IN ({placeholders})
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (instance_id, *sorted(ACTIVE_OPERATION_STATUSES)),
    ).fetchone()


def _latest_plan(db, instance_id: str):
    return db.execute(
        """
        SELECT * FROM operations
        WHERE instance_id = ? AND command = 'plan'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (instance_id,),
    ).fetchone()


def create_operation(request: OperationRequest) -> dict:
    operation_id = str(uuid.uuid4())
    instance_id = str(uuid.uuid4()) if request.command == Command.plan else request.instance_id
    effective_stack = request.stack.value if hasattr(request.stack, "value") else request.stack
    created = now()
    payload = request.model_dump(mode="json")
    access_ciphertext = None
    if request.spec is not None:
        payload.get("spec", {}).pop("root_password", None)
        if request.spec.root_password is not None:
            access_ciphertext = encrypt_secret(request.spec.root_password.get_secret_value())
    with database() as db:
        if request.command == Command.plan and request.spec is not None:
            duplicate = db.execute(
                "SELECT instance_id FROM instances WHERE name = ? AND status != 'deleted' LIMIT 1",
                (request.spec.name,),
            ).fetchone()
            if duplicate is not None:
                raise HTTPException(status_code=409, detail="ya existe una instancia con ese nombre")

        if request.command == Command.power:
            target = db.execute(
                """
                SELECT i.instance_id, i.status, o.stack
                FROM instances i
                LEFT JOIN operations o ON o.operation_id = i.operation_id
                WHERE i.instance_id = ?
                """,
                (instance_id,),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail="instance_id no existe")
            if target["status"] == "deleted":
                raise HTTPException(status_code=409, detail="la instancia ya fue eliminada")
            if target["stack"] != "mng01":
                raise HTTPException(status_code=409, detail="Power Lifecycle solo aplica al stack mng01")
            effective_stack = target["stack"]

        if request.plan_operation_id:
            source = db.execute(
                "SELECT operation_id, instance_id, stack, command, action, status FROM operations WHERE operation_id = ?",
                (request.plan_operation_id,),
            ).fetchone()
            if source is None:
                raise HTTPException(status_code=404, detail="plan_operation_id no existe")
            if request.instance_id and source["instance_id"] != request.instance_id:
                raise HTTPException(status_code=409, detail="el plan no pertenece a la instancia solicitada")
            instance_id = instance_id or source["instance_id"]
            if source["stack"]:
                effective_stack = source["stack"]
                payload["stack"] = effective_stack

            if request.command in {Command.apply, Command.destroy} and (
                source["command"] != "plan"
                or source["action"] != "provision"
                or source["status"] != "succeeded"
            ):
                raise HTTPException(status_code=409, detail="el plan todavía no está listo")

        if request.command in {Command.apply, Command.destroy} and not instance_id:
            raise HTTPException(status_code=422, detail="la operacion no tiene instancia objetivo")

        if request.command in {Command.apply, Command.destroy, Command.power} and instance_id:
            active = _active_instance_operation(db, instance_id)
            if active is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"la instancia ya tiene una operacion activa: {active['operation_id']}",
                )

        if request.command == Command.power:
            payload["stack"] = effective_stack

        payload["instance_id"] = instance_id
        db.execute(
            """
            INSERT INTO operations(
              operation_id, command, action, status, payload_json, instance_id,
              plan_operation_id, stack, access_ciphertext, access_expires_at, created_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                request.command.value,
                request.action.value,
                json.dumps(payload),
                instance_id,
                request.plan_operation_id,
                effective_stack,
                access_ciphertext,
                datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
                    seconds=int(os.getenv("IACP_ACCESS_SECRET_TTL_SECONDS", "3600"))
                ) if access_ciphertext else None,
                created,
            ),
        )
        if request.command == Command.plan and request.action == Action.provision and request.spec is not None:
            db.execute(
                """
                INSERT INTO instances(
                  instance_id, operation_id, name, image, status, power_state,
                  spec_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?, ?)
                """,
                (
                    instance_id,
                    operation_id,
                    request.spec.name,
                    request.spec.image,
                    json.dumps(payload["spec"], sort_keys=True),
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


def create_instance(request: InstanceCreateRequest) -> dict:
    return create_operation(
        OperationRequest(
            command=Command.plan,
            action=Action.provision,
            stack=request.stack,
            spec=request.spec,
        )
    )


def create_provision(instance_id: str, plan_operation_id: str | None = None) -> dict:
    with database() as db:
        plan = db.execute(
            """
            SELECT operation_id FROM operations
            WHERE instance_id = ? AND command = 'plan'
            ORDER BY created_at DESC LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
    plan_id = plan_operation_id or (plan["operation_id"] if plan else None)
    if plan_id is None:
        raise HTTPException(status_code=409, detail="la instancia no tiene un plan")
    return create_operation(
        OperationRequest(
            command=Command.apply,
            action=Action.provision,
            instance_id=instance_id,
            plan_operation_id=plan_id,
        )
    )


def create_destroy(instance_id: str) -> dict:
    with database() as db:
        instance = db.execute(
            "SELECT status, power_state FROM instances WHERE instance_id = ?",
            (instance_id,),
        ).fetchone()
        if instance is None:
            raise HTTPException(status_code=404, detail="instance not found")
        if instance["power_state"] != "stopped":
            raise HTTPException(status_code=409, detail="la instancia debe estar observada como stopped antes de destruirla")
        plan = _latest_plan(db, instance_id)
    if plan is None:
        raise HTTPException(status_code=409, detail="la instancia no tiene un plan")
    return create_operation(
        OperationRequest(
            command=Command.destroy,
            action=Action.destroy,
            instance_id=instance_id,
            plan_operation_id=plan["operation_id"],
        )
    )


def create_power(instance_id: str, action: PowerAction) -> dict:
    return create_operation(
        OperationRequest(
            command=Command.power,
            action=Action.power,
            instance_id=instance_id,
            power_action=action,
        )
    )


def operation_row(row):
    if row is None:
        raise HTTPException(status_code=404, detail="operation not found")
    result = dict(row)
    result.pop("access_ciphertext", None)
    result.pop("access_expires_at", None)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


def instance_row(row):
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    result = dict(row)
    result["spec"] = json.loads(result.pop("spec_json"))
    with database() as db:
        latest = db.execute(
            """
            SELECT operation_id, command, action, status, created_at,
                   started_at, finished_at, last_heartbeat, error
            FROM operations
            WHERE instance_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (result["instance_id"],),
        ).fetchone()
        stack = db.execute(
            "SELECT stack FROM operations WHERE operation_id = ?",
            (result["operation_id"],),
        ).fetchone()
    result["stack"] = stack["stack"] if stack else "mng01"
    result["latest_operation"] = dict(latest) if latest else None
    return result


def list_instances(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    name: str | None = None,
    include_deleted: bool = False,
):
    filters = ["1 = 1"]
    values: list[object] = []
    if status:
        filters.append("i.status = ?")
        values.append(status)
    elif not include_deleted:
        filters.append("i.status != 'deleted'")
    if name:
        filters.append("i.name = ?")
        values.append(name)
    where = " AND ".join(filters)
    with database() as db:
        total = db.execute(
            f"SELECT COUNT(*) AS count FROM instances i WHERE {where}", values
        ).fetchone()["count"]
        rows = db.execute(
            f"""
            SELECT i.* FROM instances i
            WHERE {where}
            ORDER BY i.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (*values, limit, offset),
        ).fetchall()
    return {"instances": [instance_row(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def image_catalog() -> list[dict]:
    path = os.getenv("IACP_MNG_CATALOG", "/workspace/terraform/environments/mng01/catalog.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["images"]
