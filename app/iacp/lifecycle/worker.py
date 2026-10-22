from ..db import get_session_factory
from ..worker import celery_app
from .execution import execute_power


@celery_app.task(name="iacp.kvm.power", autoretry_for=(), acks_late=True,
                 reject_on_worker_lost=True, soft_time_limit=220, time_limit=240)
def power_operation(identifier: str) -> None:
    execute_power(identifier, get_session_factory())
