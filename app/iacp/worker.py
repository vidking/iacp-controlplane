import os
from celery import Celery
from .db import SessionLocal
from .models import Job

celery_app = Celery("iacp", broker=os.environ["CELERY_BROKER_URL"])
celery_app.conf.update(task_acks_late=True, worker_prefetch_multiplier=1, task_ignore_result=True)


@celery_app.task(name="iacp.smoke", autoretry_for=(), acks_late=True)
def smoke(job_id: str) -> None:
    # Única operación permitida en el bootstrap: no ejecuta Terraform ni Proxmox.
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None or job.status == "completed":
            return
        job.status = "running"
        db.commit()
        job.status = "completed"
        job.message = "La cola, el worker y PostgreSQL funcionan; Terraform NO fue ejecutado."
        db.commit()
