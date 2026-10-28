"""Autorización técnica única del contrato ``provisioning-api-core``.

Las decisiones comerciales y las filas LabPolicy históricas no participan en
la autorización operativa. La única fuente de pertenencia es la evidencia
persistida por Terraform y su vínculo técnico unificado.
"""


def member(session, instance, action, *, current_profiles=False, observation=False):
    from .technical_ownership import member as technical_member

    return technical_member(
        session,
        instance,
        action,
        current_profiles=current_profiles,
        observation=observation,
        adopt=True,
    )


def authorize_operation(session, instance, action):
    return member(session, instance, action)


def advance(session, instance, publish):
    from .commercial import advance as technical_advance

    return technical_advance(session, instance, publish)


def advance_deletion(session, instance, operation_id, publish):
    from .commercial import advance_deletion as technical_advance_deletion

    return technical_advance_deletion(session, instance, operation_id, publish)


def after_plan(job_id, sessions, publish):
    """Consume unambiguously authorized plans without a LAB gate for Core."""
    with sessions() as session:
        from sqlalchemy import select

        from ..models import Instance, Operation, TerraformExecution
        from .instances import Conflict, event

        execution = session.scalar(
            select(TerraformExecution).where(TerraformExecution.job_id == job_id)
        )
        if execution is None or execution.status != "planned":
            return
        instance = session.get(Instance, execution.instance_id)
        if instance is None:
            return
        try:
            advance(session, instance, publish)
        except Exception as error:
            session.rollback()
            operation = session.get(Operation, execution.operation_id)
            operation.error_code = (
                str(error)[:64] if isinstance(error, Conflict) else "DISPATCH_UNCONFIRMED"
            )
            event(session, operation, "provisioning_paused", "worker:iac")
            session.commit()
