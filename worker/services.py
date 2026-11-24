"""Pool configurable de servicios con recuperación conservadora."""

import argparse
from contextlib import ExitStack
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from api.db import database, initialize, runtime_write_gate_open
from api.service import now
from api.service_catalog import enabled
from api.protection import is_protected_vmid
from worker.resilience import STOP, Backoff, identify_process, report, retry_until_ready
from worker.service_executor import (
    execute,
    ServiceBlocked,
    ServiceExecutionFailed,
    ServiceExecutionUncertain,
)
from worker.service_locks import (
    release_service_job_lock,
    service_job_execution_lock,
    try_acquire_service_job_lock,
)


TLS_RENEWAL_AGE = timedelta(days=3)
TLS_RENEWAL_FAILURE_BACKOFF = timedelta(hours=12)


def event(db, job_id, phase, status):
    db.execute(
        "INSERT INTO service_events(job_id, phase, status, occurred_at) VALUES (?, ?, ?, ?)",
        (job_id, phase, status, now()),
    )


def schedule_due_tls_renewals(db, *, current=None):
    """Encola renovación de certificados IP antes de su expiración semanal."""
    current = current or datetime.now(timezone.utc)
    renewal_cutoff = (current - TLS_RENEWAL_AGE).isoformat()
    retry_cutoff = (current - TLS_RENEWAL_FAILURE_BACKOFF).isoformat()
    rows = db.execute(
        """SELECT s.service_id, s.configuration_json, s.status AS service_status,
            i.status AS infrastructure_status, i.power_state, b.binding_json,
            (SELECT MAX(j.finished_at) FROM service_jobs j
             WHERE j.service_id=s.service_id AND j.action IN ('install','renew_tls')
               AND j.status='succeeded') AS last_success,
            (SELECT MAX(j.finished_at) FROM service_jobs j
             WHERE j.service_id=s.service_id AND j.action='renew_tls'
               AND j.status IN ('failed','blocked')) AS last_failure
        FROM service_instances s
        JOIN instances i ON i.instance_id=s.instance_id
        JOIN service_bindings b ON b.service_id=s.service_id
        WHERE s.application_id IN ('nextcloud','n8n')
          AND s.status IN ('awaiting_customer','delivered')
          AND i.status='ready' AND i.power_state='running'
          AND NOT EXISTS (
              SELECT 1 FROM service_jobs active WHERE active.service_id=s.service_id
                AND active.status IN ('queued','running','uncertain'))
          AND NOT EXISTS (
              SELECT 1 FROM operations o WHERE o.instance_id=s.instance_id
                AND o.status IN ('pending','running','planning','applying','destroying','powering','uncertain'))"""
    ).fetchall()
    enqueued = 0
    for row in rows:
        try:
            configuration = json.loads(row["configuration_json"])
            binding = json.loads(row["binding_json"])
        except (TypeError, ValueError):
            continue
        if (
            configuration.get("access_mode") != "ip"
            or not binding.get("maintenance_access_encrypted")
            or not row["last_success"]
            or row["last_success"] >= renewal_cutoff
            or (row["last_failure"] and row["last_failure"] >= retry_cutoff)
        ):
            continue
        job_id = str(uuid.uuid4())
        created_at = now()
        db.execute(
            """INSERT INTO service_jobs(job_id, service_id, action, status, created_at)
            VALUES (?, ?, 'renew_tls', 'queued', ?)""",
            (job_id, row["service_id"], created_at),
        )
        event(db, job_id, "tls_renewal_queued", "queued")
        enqueued += 1
    return enqueued


def heartbeat(worker_id, job=None):
    with database() as db:
        db.execute(
            """INSERT INTO service_workers VALUES (?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET status=excluded.status, job_id=excluded.job_id, last_heartbeat=excluded.last_heartbeat""",
            (
                worker_id,
                "running" if job else "idle",
                job["job_id"] if job else None,
                now(),
            ),
        )
        if job:
            result = db.execute(
                "UPDATE service_jobs SET last_heartbeat=? WHERE job_id=? AND generation=? AND worker_id=? AND status='running'",
                (now(), job["job_id"], job["generation"], worker_id),
            )
            if result.rowcount != 1:
                raise RuntimeError("SERVICE_EXECUTION_UNCERTAIN")


def recover():
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
    with ExitStack() as locks, database() as db:
        db.execute("BEGIN IMMEDIATE")
        deleted = db.execute("""SELECT s.service_id FROM service_instances s JOIN instances i
            ON i.instance_id=s.instance_id WHERE i.status='deleted' AND s.status != 'deleted'""").fetchall()
        for row in deleted:
            db.execute(
                "UPDATE service_instances SET status='deleted', secrets_encrypted=NULL, updated_at=? WHERE service_id=?",
                (now(), row["service_id"]),
            )
            # El binding cifra la clave privada de mantenimiento. Cuando la
            # API ya confirmó que la VM fue eliminada, esa clave deja de tener
            # un destino válido y debe salir de la base.
            db.execute(
                "DELETE FROM service_bindings WHERE service_id=?",
                (row["service_id"],),
            )
            db.execute(
                "DELETE FROM service_credential_retrievals WHERE service_id=?",
                (row["service_id"],),
            )
            db.execute(
                "UPDATE service_jobs SET status='cancelled', error='VM_DELETED', finished_at=? WHERE service_id=? AND status IN ('queued','blocked')",
                (now(), row["service_id"]),
            )
        rows = db.execute(
            "SELECT * FROM service_jobs WHERE status='running' AND last_heartbeat < ?",
            (cutoff,),
        ).fetchall()
        for job in rows:
            descriptor = try_acquire_service_job_lock(job["job_id"])
            if descriptor is None:
                # El heartbeat puede haberse retrasado mientras Ansible sigue
                # vivo. Sin adquirir el lock no se revoca la generación ni se
                # permite un segundo escritor sobre la misma VM.
                continue
            locks.callback(release_service_job_lock, descriptor)
            changed = db.execute(
                """UPDATE service_jobs SET status='uncertain', error='WORKER_HEARTBEAT_STALE'
                WHERE job_id=? AND generation=? AND status='running' AND last_heartbeat < ?""",
                (job["job_id"], job["generation"], cutoff),
            )
            if not changed.rowcount:
                continue
            if job["action"] != "renew_tls":
                db.execute(
                    "UPDATE service_instances SET status='uncertain', updated_at=? WHERE service_id=? AND status!='deleted'",
                    (now(), job["service_id"]),
                )
            event(db, job["job_id"], "recovery_required", "uncertain")
        schedule_due_tls_renewals(db)
        db.execute("COMMIT")


def claim(worker_id):
    if not enabled("IACP_SERVICES_ENABLED"):
        return None
    try:
        max_concurrent = max(1, int(os.getenv("IACP_SERVICE_MAX_CONCURRENT", "6")))
    except ValueError:
        max_concurrent = 6
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        if not runtime_write_gate_open(db):
            db.execute("COMMIT")
            return None
        active = db.execute(
            "SELECT COUNT(*) AS n FROM service_jobs WHERE status='running'"
        ).fetchone()["n"]
        if active >= max_concurrent:
            db.execute("COMMIT")
            return None
        row = db.execute("""SELECT j.*, s.status AS service_status,
            (SELECT previous.job_id FROM service_jobs previous
             WHERE previous.service_id=j.service_id AND previous.job_id<>j.job_id
               AND previous.action='install' AND previous.status='cancelled'
             ORDER BY previous.finished_at DESC LIMIT 1) AS previous_job_id
            FROM service_jobs j
            JOIN service_instances s ON s.service_id=j.service_id
            JOIN instances i ON i.instance_id=s.instance_id
            WHERE j.status='queued' AND i.status='ready' AND i.power_state='running'
            AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.instance_id=i.instance_id
                AND o.status IN ('pending','running','planning','applying','destroying','powering','uncertain'))
            ORDER BY j.created_at LIMIT 1""").fetchone()
        if not row:
            db.execute("COMMIT")
            return None
        generation = row["generation"] + 1
        db.execute(
            "UPDATE service_jobs SET status='running', worker_id=?, generation=?, last_heartbeat=?, phase='preflight' WHERE job_id=?",
            (worker_id, generation, now(), row["job_id"]),
        )
        db.execute(
            "UPDATE service_instances SET status=?, updated_at=? WHERE service_id=?",
            (
                {
                    "handoff": "delivering",
                    "cleanup": "cancelling",
                    "renew_tls": row["service_status"],
                }.get(row["action"], "installing"),
                now(),
                row["service_id"],
            ),
        )
        event(db, row["job_id"], "preflight", "running")
        db.execute("COMMIT")
        return dict(row) | {"generation": generation, "worker_id": worker_id}


def process(worker_id, job):
    with service_job_execution_lock(job["job_id"]):
        # Recovery puede ganar la carrera entre claim y adquisición del lock.
        # La comprobación de generación ocurre antes de cualquier acción remota.
        heartbeat(worker_id, job)
        _process_locked(worker_id, job)


def _process_locked(worker_id, job):
    status, error = "succeeded", ""
    try:
        with database() as db:
            service = db.execute(
                """SELECT s.*, i.vmid, i.spec_json FROM service_instances s
                JOIN instances i ON i.instance_id=s.instance_id WHERE s.service_id=?""",
                (job["service_id"],),
            ).fetchone()
        if is_protected_vmid(service["vmid"]):
            raise ServiceBlocked("PROTECTED_INSTANCE")
        execute(dict(service), job, lambda: heartbeat(worker_id, job))
    except ServiceBlocked as exc:
        status, error = "blocked", str(exc)
    except ServiceExecutionFailed as exc:
        status, error = "failed", exc.code
    except ServiceExecutionUncertain as exc:
        status, error = "uncertain", exc.code
    except Exception:
        status, error = "uncertain", "SERVICE_EXECUTION_UNCERTAIN"
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            """UPDATE service_jobs SET status=?, phase=?, error=?, finished_at=?
            WHERE job_id=? AND generation=? AND worker_id=? AND status='running'""",
            (
                status,
                "completed" if status == "succeeded" else "recovery_required",
                error,
                now(),
                job["job_id"],
                job["generation"],
                worker_id,
            ),
        )
        if changed.rowcount:
            if job["action"] == "renew_tls":
                # Estado vigente leído dentro de la transacción: la variable
                # ``service`` puede no existir si la lectura inicial falló.
                current = db.execute(
                    "SELECT status FROM service_instances WHERE service_id=?",
                    (job["service_id"],),
                ).fetchone()
                next_status = current["status"] if current else status
            elif status == "succeeded":
                next_status = {"handoff": "delivered", "cleanup": "cancelled"}.get(
                    job["action"], "awaiting_customer"
                )
            else:
                next_status = status
            db.execute(
                "UPDATE service_instances SET status=?, updated_at=? WHERE service_id=? AND status!='deleted'",
                (next_status, now(), job["service_id"]),
            )
            if next_status in {"delivered", "cancelled"}:
                db.execute(
                    "UPDATE service_instances SET secrets_encrypted=NULL WHERE service_id=?",
                    (job["service_id"],),
                )
                db.execute(
                    "DELETE FROM service_credential_retrievals WHERE service_id=?",
                    (job["service_id"],),
                )
            if status == "succeeded" and job["action"] == "cleanup":
                db.execute(
                    "DELETE FROM service_bindings WHERE service_id=?",
                    (job["service_id"],),
                )
                db.execute(
                    "DELETE FROM service_credential_retrievals WHERE service_id=?",
                    (job["service_id"],),
                )
            event(db, job["job_id"], next_status, status)
        db.execute("COMMIT")
    heartbeat(worker_id)


def run(worker_id, *, iterations=None):
    backoff = Backoff(5.0, 60.0)
    retry_until_ready(worker_id, "inicialización", initialize, backoff)
    completed = 0
    while (iterations is None or completed < iterations) and not STOP.requested:
        completed += 1
        stage = "recuperación"
        try:
            recover()
            stage = "claim"
            job = claim(worker_id)
            if job:
                stage = "ejecución"
                process(worker_id, job)
            else:
                stage = "heartbeat"
                heartbeat(worker_id)
                time.sleep(5)
            backoff.success()
        except Exception as error:  # noqa: BLE001 - se contiene y se reintenta
            delay = backoff.failure()
            report(worker_id, stage, error, delay)
            time.sleep(delay)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    worker_id = parser.parse_args().worker_id
    identify_process(worker_id)
    STOP.install(worker_id)
    run(worker_id)
