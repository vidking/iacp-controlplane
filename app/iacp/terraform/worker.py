from ..db import get_session_factory
from ..worker import celery_app
from .lifecycle import execute_plan


@celery_app.task(name="iacp.terraform.plan", autoretry_for=(), acks_late=True)
def plan_instance(job_id: str, attempt: int) -> None:
    execute_plan(job_id, attempt, get_session_factory())


@celery_app.task(name="iacp.terraform.apply", autoretry_for=(), acks_late=True, soft_time_limit=910, time_limit=930)
def apply_instance(identifier: str, mode: str = "apply") -> None:
    from .apply import execute_apply
    if mode not in {"apply", "reconcile"}:
        return
    execute_apply(identifier, get_session_factory(), reconcile=mode == "reconcile")


@celery_app.task(name="iacp.terraform.delete", autoretry_for=(), acks_late=True,
                 reject_on_worker_lost=True, soft_time_limit=910, time_limit=930)
def delete_instance(identifier: str, mode: str) -> None:
    from .deletion import execute_deletion
    execute_deletion(identifier, mode, get_session_factory())


@celery_app.task(name='iacp.network.discover',autoretry_for=(),acks_late=True,reject_on_worker_lost=True)
def discover_network(identifier: str) -> None:
    from ..services.network import execute_discovery
    execute_discovery(identifier,get_session_factory())
