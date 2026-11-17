"""Pool de servicios: PostgreSQL, dos slots y recuperación conservadora."""

import argparse
import time
from datetime import datetime, timedelta, timezone
from api.db import database, initialize
from api.service import now
from api.service_catalog import enabled
from api.protection import is_protected_vmid
from worker.service_executor import execute, ServiceBlocked


def event(db, job_id, phase, status):
    db.execute(
        "INSERT INTO service_events(job_id, phase, status, occurred_at) VALUES (?, ?, ?, ?)",
        (job_id, phase, status, now()),
    )


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
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        deleted = db.execute("""SELECT s.service_id FROM service_instances s JOIN instances i
            ON i.instance_id=s.instance_id WHERE i.status='deleted' AND s.status != 'deleted'""").fetchall()
        for row in deleted:
            db.execute(
                "UPDATE service_instances SET status='deleted', secrets_encrypted=NULL, updated_at=? WHERE service_id=?",
                (now(), row["service_id"]),
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
            db.execute(
                "UPDATE service_jobs SET status='uncertain', error='WORKER_HEARTBEAT_STALE' WHERE job_id=?",
                (job["job_id"],),
            )
            db.execute(
                "UPDATE service_instances SET status='uncertain', updated_at=? WHERE service_id=?",
                (now(), job["service_id"]),
            )
            event(db, job["job_id"], "recovery_required", "uncertain")
        db.execute("COMMIT")


def claim(worker_id):
    if not enabled("IACP_SERVICES_ENABLED"):
        return None
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT COUNT(*) AS n FROM service_jobs WHERE status='running'"
        ).fetchone()["n"]
        if active >= 2:
            db.execute("COMMIT")
            return None
        row = db.execute("""SELECT j.* FROM service_jobs j
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
                "delivering" if row["action"] == "handoff" else "installing",
                now(),
                row["service_id"],
            ),
        )
        event(db, row["job_id"], "preflight", "running")
        db.execute("COMMIT")
        return dict(row) | {"generation": generation, "worker_id": worker_id}


def process(worker_id, job):
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
            next_status = (
                ("delivered" if job["action"] == "handoff" else "awaiting_customer")
                if status == "succeeded"
                else status
            )
            db.execute(
                "UPDATE service_instances SET status=?, updated_at=? WHERE service_id=?",
                (next_status, now(), job["service_id"]),
            )
            if next_status == "delivered":
                db.execute(
                    "UPDATE service_instances SET secrets_encrypted=NULL WHERE service_id=?",
                    (job["service_id"],),
                )
            event(db, job["job_id"], next_status, status)
        db.execute("COMMIT")
    heartbeat(worker_id)


def run(worker_id):
    initialize()
    while True:
        recover()
        job = claim(worker_id)
        if job:
            process(worker_id, job)
        else:
            heartbeat(worker_id)
            time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    run(parser.parse_args().worker_id)
