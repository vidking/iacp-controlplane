"""Pedidos transaccionales y entrega explícita de aplicaciones."""

import json
import uuid
from fastapi import HTTPException
from api.db import database
from api.models import Command, OperationRequest
from api.service import (
    _idempotent_mutation,
    _insert_operation,
    _resolve_public_request,
    now,
)
from api.service_catalog import enabled
from api.service_models import ServiceCreateRequest
from api.service_secrets import encrypt, installation_credentials
from api.secrets import write_secret, delete_secret


ACTIVE = "('queued','running','uncertain')"


def require_enabled():
    if not enabled("IACP_SERVICES_ENABLED"):
        raise HTTPException(503, "SERVICES_DISABLED")


def guard_instance(db, instance_id: str):
    row = db.execute(
        f"""SELECT j.job_id FROM service_jobs j
        JOIN service_instances s ON s.service_id=j.service_id
        WHERE s.instance_id=? AND j.status IN {ACTIVE} LIMIT 1""",
        (instance_id,),
    ).fetchone()
    if row:
        raise HTTPException(409, "SERVICE_OPERATION_ACTIVE")


def create(request: ServiceCreateRequest, key: str | None):
    require_enabled()
    # El hash de idempotencia incluye los secretos, nunca la respuesta ni el payload público.
    body = request.model_dump(mode="json")
    body["secrets"] = {
        k: v.get_secret_value() if v else None
        for k, v in request.secrets.__dict__.items()
    }
    if request.instance.access.root_password:
        body["instance"]["access"]["root_password"] = (
            request.instance.access.root_password.get_secret_value()
        )

    def builder(db):
        service_id, job_id, operation_id = (str(uuid.uuid4()) for _ in range(3))
        credentials = installation_credentials()
        credentials.update(body["secrets"])
        try:
            envelope = encrypt(credentials)
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(503, "SERVICE_KEY_NOT_CONFIGURED") from None
        instance = request.instance.model_copy(deep=True)
        instance.access.ssh_public_keys.append(credentials["ssh_public_key"])
        instance.tags = list(
            dict.fromkeys(instance.tags + [f"app-{request.application_id}"])
        )
        spec, plan, _image, password, environment = _resolve_public_request(instance)
        spec.dns_servers = ["1.1.1.1", "8.8.8.8"]
        secret_path = str(write_secret(operation_id, password)) if password else None
        try:
            response, _ = _insert_operation(
                db,
                OperationRequest(
                    command=Command.provision,
                    plan_id=plan["plan_id"],
                    spec=spec,
                    environment=environment,
                ),
                commercial_context=instance.commercial_context.model_dump()
                if instance.commercial_context
                else None,
                secret_path=secret_path,
                operation_id=operation_id,
                environment=environment,
            )
            db.execute(
                """INSERT INTO service_instances VALUES (?, ?, ?, '1', ?, ?, 'waiting_vm', NULL, ?, ?)""",
                (
                    service_id,
                    response["instance_id"],
                    request.application_id,
                    request.configuration.model_dump_json(),
                    envelope,
                    now(),
                    now(),
                ),
            )
            db.execute(
                """INSERT INTO service_jobs(job_id, service_id, status, created_at)
                VALUES (?, ?, 'queued', ?)""",
                (job_id, service_id, now()),
            )
        except Exception:
            delete_secret(secret_path)
            raise
        return dict(
            service_id=service_id,
            job_id=job_id,
            instance_id=response["instance_id"],
            operation_id=operation_id,
            status="waiting_vm",
        ), operation_id

    return _idempotent_mutation(
        scope="POST:/v1/service-instances",
        idempotency_key=key,
        body=body,
        builder=builder,
    )


def get_service(service_id):
    with database() as db:
        row = db.execute(
            """SELECT s.service_id, s.instance_id, s.application_id, s.recipe_version,
            s.configuration_json, s.status, s.created_at, s.updated_at, i.status AS infrastructure_status,
            i.power_state, i.vmid FROM service_instances s JOIN instances i ON i.instance_id=s.instance_id
            WHERE s.service_id=?""",
            (service_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "SERVICE_NOT_FOUND")
        result = dict(row)
        result["configuration"] = json.loads(result.pop("configuration_json"))
        result["jobs"] = [
            dict(job)
            for job in db.execute(
                "SELECT * FROM service_jobs WHERE service_id=? ORDER BY created_at",
                (service_id,),
            ).fetchall()
        ]
        spec = db.execute("SELECT spec_json FROM instances WHERE instance_id=?", (result["instance_id"],)).fetchone()
        if result["application_id"] in {"nextcloud", "n8n"}:
            from ipaddress import ip_interface
            host = result["configuration"].get("domain")
            if result["configuration"].get("access_mode") == "ip" and spec:
                cidr = json.loads(spec["spec_json"]).get("ipv4_cidr")
                host = str(ip_interface(cidr).ip) if cidr else None
            result["access_url"] = "https://" + host + "/" if host else None
        return result


def get_job(job_id):
    with database() as db:
        row = db.execute(
            "SELECT * FROM service_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "SERVICE_JOB_NOT_FOUND")
        return dict(row) | {
            "events": [
                dict(e)
                for e in db.execute(
                    "SELECT * FROM service_events WHERE job_id=? ORDER BY event_id",
                    (job_id,),
                ).fetchall()
            ]
        }


def retry(job_id, key, stopped=False):
    require_enabled()

    def builder(db):
        job = db.execute(
            "SELECT * FROM service_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not job:
            raise HTTPException(404, "SERVICE_JOB_NOT_FOUND")
        if job["status"] not in {"failed", "blocked", "uncertain"}:
            raise HTTPException(409, "SERVICE_NOT_RETRYABLE")
        if job["status"] == "uncertain" and not stopped:
            raise HTTPException(409, "PREVIOUS_EXECUTION_MUST_BE_STOPPED")
        service = db.execute(
            "SELECT instance_id FROM service_instances WHERE service_id=?",
            (job["service_id"],),
        ).fetchone()
        active = db.execute(
            "SELECT 1 FROM operations WHERE instance_id=? AND status IN ('pending','running','planning','applying','destroying','powering','uncertain')",
            (service["instance_id"],),
        ).fetchone()
        if active:
            raise HTTPException(409, "INFRASTRUCTURE_OPERATION_ACTIVE")
        db.execute(
            "UPDATE service_jobs SET status='queued', error='', worker_id=NULL, finished_at=NULL WHERE job_id=?",
            (job_id,),
        )
        return {"job_id": job_id, "status": "queued"}, None

    return _idempotent_mutation(
        scope=f"POST:service-jobs:{job_id}:retry",
        idempotency_key=key,
        body={"stopped": stopped},
        builder=builder,
    )


def delivery(service_id, key):
    require_enabled()

    def builder(db):
        row = db.execute(
            "SELECT * FROM service_instances WHERE service_id=?", (service_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "SERVICE_NOT_FOUND")
        if row["status"] != "awaiting_customer" or not row["credentials_retrieved_at"]:
            raise HTTPException(409, "SERVICE_NOT_READY_FOR_DELIVERY")
        guard_instance(db, row["instance_id"])
        active = db.execute(
            "SELECT 1 FROM operations WHERE instance_id=? AND status IN ('pending','running','planning','applying','destroying','powering','uncertain')",
            (row["instance_id"],),
        ).fetchone()
        if active:
            raise HTTPException(409, "INFRASTRUCTURE_OPERATION_ACTIVE")
        job_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO service_jobs(job_id, service_id, action, status, created_at) VALUES (?, ?, 'handoff', 'queued', ?)",
            (job_id, service_id, now()),
        )
        return {"job_id": job_id, "status": "queued"}, None

    return _idempotent_mutation(
        scope=f"POST:service-instances:{service_id}:deliver",
        idempotency_key=key,
        body={},
        builder=builder,
    )


def credentials(service_id):
    from api.service_secrets import decrypt

    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM service_instances WHERE service_id=?", (service_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "SERVICE_NOT_FOUND")
        if row["status"] != "awaiting_customer" or row["credentials_retrieved_at"]:
            raise HTTPException(409, "CREDENTIALS_UNAVAILABLE")
        secrets = decrypt(row["secrets_encrypted"])
        if row["application_id"] == "nextcloud":
            result = {
                "admin_user": json.loads(row["configuration_json"])["admin_user"],
                "admin_password": secrets["admin_password"],
            }
        elif row["application_id"] == "n8n":
            result = {
                "onboarding_username": "installer",
                "onboarding_password": secrets["admin_password"],
                "owner_setup_required": True,
            }
        elif row["application_id"] == "openclaw":
            result = {"gateway_token": secrets["gateway_token"], "access": "ssh"}
        else:
            result = {
                "access": "ssh",
                "telegram_configured": bool(secrets.get("telegram_bot_token")),
            }
        db.execute(
            "UPDATE service_instances SET credentials_retrieved_at=? WHERE service_id=?",
            (now(), service_id),
        )
        db.execute("COMMIT")
        return result


def cancel(job_id, key):
    def builder(db):
        row = db.execute(
            "SELECT * FROM service_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "SERVICE_JOB_NOT_FOUND")
        if row["status"] not in {"queued", "blocked"}:
            raise HTTPException(409, "SERVICE_CANNOT_CANCEL_RUNNING_OR_UNCERTAIN")
        db.execute(
            "UPDATE service_jobs SET status='cancelled', finished_at=? WHERE job_id=?",
            (now(), job_id),
        )
        db.execute(
            "UPDATE service_instances SET status='cancelled', updated_at=? WHERE service_id=?",
            (now(), row["service_id"]),
        )
        return {"job_id": job_id, "status": "cancelled"}, None

    return _idempotent_mutation(
        scope=f"POST:service-jobs:{job_id}:cancel",
        idempotency_key=key,
        body={},
        builder=builder,
    )


def connections(service_id, request, key):
    require_enabled()
    from api.service_secrets import decrypt

    body = {
        "secrets": {
            k: v.get_secret_value() if v else None
            for k, v in request.secrets.__dict__.items()
        },
        "telegram_allowed_users": request.telegram_allowed_users,
    }

    def builder(db):
        service = db.execute(
            "SELECT * FROM service_instances WHERE service_id=?", (service_id,)
        ).fetchone()
        if not service:
            raise HTTPException(404, "SERVICE_NOT_FOUND")
        if service["application_id"] not in {"hermes", "openclaw"} or service[
            "status"
        ] not in {"awaiting_customer", "blocked"}:
            raise HTTPException(409, "SERVICE_CONNECTION_NOT_EDITABLE")
        guard_instance(db, service["instance_id"])
        configuration = json.loads(service["configuration_json"])
        credentials = decrypt(service["secrets_encrypted"])
        credentials.update({k: v for k, v in body["secrets"].items() if v is not None})
        if request.telegram_allowed_users is not None:
            configuration["telegram_allowed_users"] = request.telegram_allowed_users
        if credentials.get("telegram_bot_token") and (
            service["application_id"] != "hermes"
            or not configuration["telegram_allowed_users"]
        ):
            raise HTTPException(422, "TELEGRAM_REQUIRES_HERMES_AND_ALLOWED_USERS")
        job_id = str(uuid.uuid4())
        db.execute(
            "UPDATE service_instances SET configuration_json=?, secrets_encrypted=?, status='queued', updated_at=? WHERE service_id=?",
            (json.dumps(configuration), encrypt(credentials), now(), service_id),
        )
        db.execute(
            "INSERT INTO service_jobs(job_id, service_id, status, created_at) VALUES (?, ?, 'queued', ?)",
            (job_id, service_id, now()),
        )
        return {"job_id": job_id, "status": "queued"}, None

    return _idempotent_mutation(
        scope=f"POST:service-instances:{service_id}:connections",
        idempotency_key=key,
        body=body,
        builder=builder,
    )
