from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..models import Job

Publisher = Callable[[str], object]


class QueueUnavailable(Exception):
    pass


def create_smoke_job(session: Session, publish: Publisher) -> Job:
    job = Job(id=str(uuid4()), operation="smoke", status="queued")
    session.add(job)
    session.commit()
    try:
        publish(job.id)
    except Exception:
        # A publish timeout can be ambiguous. Do not overwrite a worker that
        # already advanced the job. Outbox/reconciliation belongs to increment 3.
        session.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == "queued")
            .values(status="failed", message="Error publicando trabajo en la cola")
        )
        session.commit()
        raise QueueUnavailable from None
    return job
