"""Pedidos transaccionales y entrega explícita de aplicaciones."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
import json
from secrets import token_urlsafe
import uuid
from fastapi import HTTPException
from api.db import database
from api.models import Command, OperationRequest
from api.service import (
    _idempotent_mutation,
    _idempotency_key_or_error,
    _insert_operation,
    _resolve_public_request,
    emit,
    now,
)
from api.service_catalog import enabled, supported_image_ids
from api.service_models import ServiceCreateRequest
from api.service_secrets import encrypt, installation_credentials
from api.secrets import write_secret, delete_secret


ACTIVE = "('queued','running','uncertain')"
CREDENTIAL_RECOVERY_WINDOW = timedelta(minutes=15)


def require_enabled():
    if not enabled("IACP_SERVICES_ENABLED"):
        raise HTTPException(503, "SERVICES_DISABLED")


def guard_instance(db, instance_id: str, *, allow_queued: bool = False):
    # allow_queued: un job en cola no se ejecuta mientras la instancia tenga
    # una operación activa o uncertain (ver worker.services.claim). Bloquear
    # la reconciliación por él producía un bloqueo mutuo: reconcile exigía
    # que no hubiera jobs y el retry del job exigía que no hubiera uncertain.
    statuses = "('running','uncertain')" if allow_queued else ACTIVE
    row = db.execute(
        f"""SELECT j.job_id FROM service_jobs j
        JOIN service_instances s ON s.service_id=j.service_id
        WHERE s.instance_id=? AND j.status IN {statuses} LIMIT 1""",
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
        credentials.update(
            {name: value for name, value in body["secrets"].items() if value is not None}
        )
        configuration = request.configuration.model_dump()
        if request.application_id == "n8n":
            credentials.pop("admin_password", None)
            if not credentials.get("owner_password"):
                configuration.update(
                    owner_email=f"owner-{service_id}@example.invalid",
                    owner_first_name="Cuenta",
                    owner_last_name="Administradora",
                )
                credentials["owner_password"] = "Aa1-" + token_urlsafe(32)
        elif request.application_id == "nextcloud" and request.secrets.admin_password:
            credentials["admin_password"] = request.secrets.admin_password.get_secret_value()
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
                """INSERT INTO service_instances(
                    service_id, instance_id, application_id, recipe_version,
                    configuration_json, secrets_encrypted, status, created_at, updated_at
                ) VALUES (?, ?, ?, '1', ?, ?, 'waiting_vm', ?, ?)""",
                (
                    service_id,
                    response["instance_id"],
                    request.application_id,
                    json.dumps(configuration),
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


def list_services(limit=20, offset=0):
    with database() as db:
        total = db.execute(
            """SELECT COUNT(*) AS total FROM service_instances s
            JOIN instances i ON i.instance_id=s.instance_id
            WHERE s.status!='deleted' AND i.status!='deleted'"""
        ).fetchone()["total"]
        rows = db.execute(
            """SELECT s.service_id, s.instance_id, s.application_id, s.status,
            i.name, i.vmid, i.status AS infrastructure_status
            FROM service_instances s JOIN instances i ON i.instance_id=s.instance_id
            WHERE s.status!='deleted' AND i.status!='deleted'
            ORDER BY s.created_at DESC, s.service_id
            LIMIT ? OFFSET ?""", (limit, offset),
        ).fetchall()
    return {"services": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_service(service_id):
    with database() as db:
        row = db.execute(
            """SELECT s.service_id, s.instance_id, s.application_id, s.recipe_version,
            s.configuration_json, s.status, s.created_at, s.updated_at, s.credentials_retrieved_at,
            i.name, i.status AS infrastructure_status,
                   i.power_state, i.vmid, r.request_hash IS NOT NULL AS credential_receipt_required
            FROM service_instances s JOIN instances i ON i.instance_id=s.instance_id
            LEFT JOIN service_credential_retrievals r ON r.service_id=s.service_id
            WHERE s.service_id=?""",
            (service_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "SERVICE_NOT_FOUND")
        result = dict(row)
        retrieved_at = result.pop("credentials_retrieved_at")
        awaiting_customer = result["status"] == "awaiting_customer"
        result["credentials_available"] = retrieved_at is None and awaiting_customer
        result["credentials_recovery_possible"] = retrieved_at is not None and awaiting_customer
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
                if not host:
                    binding = db.execute("SELECT binding_json FROM service_bindings WHERE service_id=?", (service_id,)).fetchone()
                    host = json.loads(binding["binding_json"]).get("address") if binding else None
            # El endpoint web no es utilizable hasta que la receta terminó de
            # configurar la aplicación (incluidos trusted_domains en
            # Nextcloud). Evita que el consumidor abra una IP intermedia y vea
            # una pantalla de instalación o de dominio no confiable.
            owner_review_required = (
                result["application_id"] == "n8n"
                and awaiting_customer
                and not result["configuration"].get("owner_email")
            )
            if owner_review_required:
                result["credentials_available"] = False
                result["credentials_recovery_possible"] = False
            result["access_url"] = (
                "https://" + host + "/"
                if host and result["status"] in {"awaiting_customer", "delivered"} and not owner_review_required
                else None
            )
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


def _current_job(db, job):
    latest = db.execute(
        f"""SELECT job_id FROM service_jobs WHERE service_id=?
        ORDER BY (status IN {ACTIVE}) DESC, created_at DESC, job_id DESC LIMIT 1""",
        (job["service_id"],),
    ).fetchone()
    if latest["job_id"] != job["job_id"]:
        raise HTTPException(409, "SERVICE_JOB_SUPERSEDED")
    service = db.execute(
        """SELECT s.instance_id, s.application_id, s.status, i.status AS infrastructure_status,
        i.image, i.power_state, i.operation_id, i.vmid
        FROM service_instances s JOIN instances i ON i.instance_id=s.instance_id
        WHERE s.service_id=?""",
        (job["service_id"],),
    ).fetchone()
    if service["status"] in {"cancelled", "delivered", "deleted"}:
        raise HTTPException(409, "SERVICE_JOB_SUPERSEDED")
    return service


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
        service = _current_job(db, job)
        if job["action"] == "install" and service["image"] not in supported_image_ids(service["application_id"]):
            raise HTTPException(409, "SERVICE_IMAGE_RETIRED")
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


def delivery(service_id, key, receipt=None):
    require_enabled()

    def builder(db):
        row = db.execute(
            """SELECT s.*, r.request_hash AS credentials_retrieval_key_hash
            FROM service_instances s LEFT JOIN service_credential_retrievals r USING(service_id)
            WHERE s.service_id=?""",
            (service_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "SERVICE_NOT_FOUND")
        if row["status"] != "awaiting_customer" or not row["credentials_retrieved_at"]:
            raise HTTPException(409, "SERVICE_NOT_READY_FOR_DELIVERY")
        if row["credentials_retrieval_key_hash"]:
            from api.service_secrets import decrypt

            expected = decrypt(row["secrets_encrypted"]).get("credential_receipt")
            if not expected or not receipt or not compare_digest(
                sha256(receipt.encode()).digest(), sha256(expected.encode()).digest()
            ):
                raise HTTPException(409, "SERVICE_CREDENTIAL_RECEIPT_REQUIRED")
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
        body={"receipt_hash": sha256(receipt.encode()).hexdigest()} if receipt else {},
        builder=builder,
    )


def _credential_recovery_open(retrieved_at):
    return datetime.now(timezone.utc) < (
        datetime.fromisoformat(retrieved_at) + CREDENTIAL_RECOVERY_WINDOW
    )


def credentials(service_id, key=None):
    from api.service_secrets import decrypt

    key_hash = sha256(_idempotency_key_or_error(key).encode()).hexdigest() if key else None
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT s.*, r.request_hash AS credentials_retrieval_key_hash
            FROM service_instances s LEFT JOIN service_credential_retrievals r USING(service_id)
            WHERE s.service_id=?""",
            (service_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "SERVICE_NOT_FOUND")
        if row["status"] != "awaiting_customer":
            raise HTTPException(409, "CREDENTIALS_UNAVAILABLE")
        guard_instance(db, row["instance_id"])
        previous_key_hash = row["credentials_retrieval_key_hash"]
        recovered = False
        if row["credentials_retrieved_at"]:
            if not key_hash:
                raise HTTPException(409, "CREDENTIALS_UNAVAILABLE")
            if _credential_recovery_open(row["credentials_retrieved_at"]):
                if not previous_key_hash or not compare_digest(key_hash, previous_key_hash):
                    raise HTTPException(409, "CREDENTIALS_UNAVAILABLE")
                recovered = True
        secrets = decrypt(row["secrets_encrypted"])
        if row["application_id"] == "nextcloud":
            result = {
                "admin_user": json.loads(row["configuration_json"])["admin_user"],
                "admin_password": secrets["admin_password"],
            }
        elif row["application_id"] == "n8n":
            configuration = json.loads(row["configuration_json"])
            if not secrets.get("owner_password") or not configuration.get("owner_email"):
                raise HTTPException(409, "SERVICE_N8N_OWNER_REQUIRES_REVIEW")
            result = {
                "owner_email": configuration["owner_email"],
                "owner_first_name": configuration["owner_first_name"],
                "owner_last_name": configuration["owner_last_name"],
                "owner_password": secrets["owner_password"],
                "owner_setup_required": False,
            }
        elif row["application_id"] == "openclaw":
            result = {"gateway_token": secrets["gateway_token"], "access": "ssh"}
        else:
            result = {
                "access": "ssh",
                "telegram_configured": bool(secrets.get("telegram_bot_token")),
            }
        if key_hash:
            if not recovered:
                secrets["credential_receipt"] = token_urlsafe(32)
            result["credential_receipt"] = secrets["credential_receipt"]
        if not recovered:
            db.execute(
                "UPDATE service_instances SET credentials_retrieved_at=?, secrets_encrypted=? WHERE service_id=?",
                (now(), encrypt(secrets) if key_hash else row["secrets_encrypted"], service_id),
            )
            if key_hash:
                db.execute(
                    """INSERT INTO service_credential_retrievals(service_id, request_hash) VALUES (?, ?)
                    ON CONFLICT(service_id) DO UPDATE SET request_hash=excluded.request_hash""",
                    (service_id, key_hash),
                )
        db.execute("COMMIT")
        return result


def cancel(job_id, key, stopped=False):
    def builder(db):
        row = db.execute(
            "SELECT * FROM service_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "SERVICE_JOB_NOT_FOUND")
        if row["status"] == "cancelled":
            operation = db.execute(
                """SELECT o.operation_id
                FROM service_instances s JOIN instances i ON i.instance_id=s.instance_id
                JOIN operations o ON o.operation_id=i.operation_id
                WHERE s.service_id=? AND s.status='cancelled' AND o.phase='service_cancelled'""",
                (row["service_id"],),
            ).fetchone()
            if operation:
                operation_id = operation["operation_id"]
                return {"job_id": job_id, "status": "cancelled", "operation_id": operation_id}, operation_id
        if row["status"] not in {"queued", "blocked"} and not (
            row["status"] == "uncertain" and stopped
        ):
            raise HTTPException(409, "SERVICE_CANNOT_CANCEL_RUNNING_OR_UNCERTAIN")
        service = _current_job(db, row)
        if row["action"] == "handoff" and row["status"] == "queued" and row["generation"] == 0:
            db.execute("UPDATE service_jobs SET status='cancelled', finished_at=? WHERE job_id=?", (now(), job_id))
            return {"job_id": job_id, "status": "cancelled"}, None
        if row["action"] != "install":
            raise HTTPException(409, "SERVICE_CANNOT_CANCEL_RECOVERY")

        cancelled_operation_id = None
        if service["infrastructure_status"] == "pending" and service["vmid"] is None:
            updated = db.execute(
                """UPDATE operations SET status='failed', phase='service_cancelled',
                error='SERVICE_CANCELLED', finished_at=?
                WHERE operation_id=? AND instance_id=? AND command='provision'
                AND status='pending' AND worker_id IS NULL AND vmid IS NULL AND started_at IS NULL
                AND attempt_count=0 AND workspace_path IS NULL""",
                (now(), service["operation_id"], service["instance_id"]),
            )
            if updated.rowcount:
                cancelled_operation_id = service["operation_id"]
                db.execute(
                    "UPDATE instances SET status='deleted', updated_at=? WHERE instance_id=?",
                    (now(), service["instance_id"]),
                )
                emit(db, cancelled_operation_id, "operation.cancelled", "failed", "SERVICE_CANCELLED")
        clean_without_vm = cancelled_operation_id or service["infrastructure_status"] == "deleted"
        if not clean_without_vm:
            active = db.execute(
                """SELECT 1 FROM operations WHERE instance_id=?
                AND status IN ('pending','running','planning','applying','destroying','powering','uncertain') LIMIT 1""",
                (service["instance_id"],),
            ).fetchone()
            if active:
                raise HTTPException(409, "INFRASTRUCTURE_OPERATION_ACTIVE")
            if service["infrastructure_status"] != "ready" or service["power_state"] != "running":
                raise HTTPException(409, "SERVICE_CLEANUP_REQUIRES_RUNNING_VM")
        db.execute(
            "UPDATE service_jobs SET status='cancelled', finished_at=? WHERE job_id=?",
            (now(), job_id),
        )
        if not clean_without_vm:
            cleanup_job_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO service_jobs(job_id, service_id, action, status, created_at)
                VALUES (?, ?, 'cleanup', 'queued', ?)""",
                (cleanup_job_id, row["service_id"], now()),
            )
            db.execute(
                "UPDATE service_instances SET status='cancelling', updated_at=? WHERE service_id=?",
                (now(), row["service_id"]),
            )
            return {"job_id": cleanup_job_id, "cancelled_job_id": job_id, "status": "queued"}, None
        db.execute(
            """UPDATE service_instances SET status='cancelled', secrets_encrypted=NULL, updated_at=?
            WHERE service_id=?""",
            (now(), row["service_id"]),
        )
        result = {"job_id": job_id, "status": "cancelled"}
        if cancelled_operation_id:
            result["operation_id"] = cancelled_operation_id
        return result, cancelled_operation_id

    response = _idempotent_mutation(
        scope=f"POST:service-jobs:{job_id}:cancel",
        idempotency_key=key,
        body={"stopped": True} if stopped else {},
        builder=builder,
    )
    if operation_id := response.get("operation_id"):
        # Se limpia tras confirmar la cancelación. Repetir la misma petición
        # también completa la limpieza si la respuesta o el proceso se perdieron.
        with database() as db:
            operation = db.execute(
                "SELECT secret_path FROM operations WHERE operation_id=? AND phase='service_cancelled'",
                (operation_id,),
            ).fetchone()
            if operation and operation["secret_path"]:
                delete_secret(operation["secret_path"])
                db.execute("UPDATE operations SET secret_path=NULL WHERE operation_id=?", (operation_id,))
    return response


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
