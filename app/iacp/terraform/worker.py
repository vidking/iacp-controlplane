from ..db import get_session_factory
from ..worker import celery_app
from .lifecycle import execute_plan


@celery_app.task(name="iacp.terraform.plan", autoretry_for=(), acks_late=True)
def plan_instance(job_id: str, attempt: int) -> None:
    execute_plan(job_id, attempt, get_session_factory())
    from ..services.authorization import after_plan
    from ..api.dependencies import get_apply_publisher
    after_plan(job_id, get_session_factory(), get_apply_publisher())


@celery_app.task(name="iacp.terraform.apply", autoretry_for=(), acks_late=True, soft_time_limit=910, time_limit=930)
def apply_instance(identifier: str, mode: str = "apply") -> None:
    from .apply import execute_apply
    if mode not in {"apply", "reconcile"}:
        return
    execute_apply(identifier, get_session_factory(), reconcile=mode == "reconcile")
    from ..services.fulfillment import after_apply
    after_apply(identifier, get_session_factory())


@celery_app.task(name="iacp.terraform.delete", autoretry_for=(), acks_late=True,
                 reject_on_worker_lost=True, soft_time_limit=910, time_limit=930)
def delete_instance(identifier: str, mode: str) -> None:
    from .deletion import execute_deletion
    execute_deletion(identifier, mode, get_session_factory())
    if mode == "plan":
        from ..models import DeletionExecution, Instance, Operation
        from ..services.authorization import advance_deletion
        from ..services.instances import event
        from ..api.dependencies import get_deletion_publisher
        with get_session_factory()() as session:
            row = session.get(DeletionExecution, identifier)
            if row and row.autonomous and row.status == "planned":
                try:
                    advance_deletion(session, session.get(Instance, row.instance_id), identifier, get_deletion_publisher())
                except Exception:
                    session.rollback()
                    event(session, session.get(Operation, identifier), "technical_delete_paused", "worker:iac")
                    session.commit()


@celery_app.task(name='iacp.network.discover',autoretry_for=(),acks_late=True,reject_on_worker_lost=True)
def discover_network(identifier: str) -> None:
    from ..services.network import execute_discovery
    execute_discovery(identifier,get_session_factory())


@celery_app.task(name='iacp.guest.initialize',autoretry_for=(),acks_late=True,reject_on_worker_lost=True,
                 soft_time_limit=250,time_limit=270,bind=True,max_retries=8)
def initialize_guest(task, identifier: str) -> None:
    from ..services.guest_identity import execute_initialization
    execute_initialization(identifier,get_session_factory())
    # after_power puede conservar brevemente el lock de la instancia mientras
    # publica esta tarea. Si la primera entrega observa ese lock, la operación
    # sigue queued y nunca consumió guest-exec; reencolar la misma operación
    # evita dejarla huérfana sin reabrir un fence consumido.
    from ..models import GuestInitialization
    with get_session_factory()() as session:
        initialization=session.get(GuestInitialization,identifier)
        queued=initialization is not None and initialization.status=='queued'
    if queued:
        raise task.retry(countdown=2)
    from ..services.access import after_initialization
    after_initialization(identifier, get_session_factory())
    from ..services.network import after_guest_initialization
    after_guest_initialization(identifier, get_session_factory())


@celery_app.task(name='iacp.access.verify', autoretry_for=(), acks_late=True,
                 reject_on_worker_lost=True, soft_time_limit=55, time_limit=65)
def verify_access(identifier: str) -> None:
    from ..services.access import execute_verification
    execute_verification(identifier, get_session_factory())


@celery_app.task(name='iacp.commercial.bootstrap',autoretry_for=(),acks_late=True,
    reject_on_worker_lost=True,soft_time_limit=90,time_limit=110)
def bootstrap_root(identifier: str) -> None:
    from ..services.commercial_bootstrap import execute
    execute(identifier,get_session_factory())
