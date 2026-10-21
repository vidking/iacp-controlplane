from ..db import get_session_factory
from ..worker import celery_app
from .lifecycle import execute_plan


@celery_app.task(name="iacp.terraform.plan", autoretry_for=(), acks_late=True)
def plan_instance(job_id: str, attempt: int) -> None:
    execute_plan(job_id, attempt, get_session_factory())
