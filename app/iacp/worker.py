from celery import Celery
from .config import broker_url
from .db import get_session_factory
from .models import Job

celery_app = Celery("iacp", broker=broker_url())
celery_app.conf.update(
    task_acks_late=True, worker_prefetch_multiplier=1, task_ignore_result=True,
    broker_connection_timeout=3, broker_connection_retry_on_startup=True,
    task_publish_retry=False,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
)


@celery_app.task(name="iacp.smoke", autoretry_for=(), acks_late=True)
def smoke(job_id: str) -> None:
    # Única operación permitida en el bootstrap: no ejecuta Terraform ni Proxmox.
    with get_session_factory()() as db:
        job = db.get(Job, job_id)
        if job is None or job.operation != "smoke" or job.status not in {"queued", "running"}:
            return
        job.status = "running"
        db.commit()
        job.status = "completed"
        job.message = "La cola, el worker y PostgreSQL funcionan; Terraform NO fue ejecutado."
        db.commit()
