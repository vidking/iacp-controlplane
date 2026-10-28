"""Autorización de mutaciones según el contrato de la instancia.

Las instancias Core nunca consultan LabPolicy. El import del motor LAB queda
limitado a recursos históricos que todavía conservan ``self_service_policy_id``.
"""

from .contracts import instance_uses_managed_contract


def member(session, instance, action, *, current_profiles=False, observation=False):
    if instance_uses_managed_contract(instance):
        if observation:
            from .commercial import member as commercial_member

            return commercial_member(
                session,
                instance,
                action,
                current_profiles=current_profiles,
                observation=True,
            )
        from .commercial import delegation

        return delegation(
            session,
            instance,
            action,
            current_profiles=current_profiles,
        )
    from .lab import member as historical_member

    return historical_member(
        session,
        instance,
        action,
        current_profiles=current_profiles,
    )


def authorize_operation(session, instance, action):
    if instance_uses_managed_contract(instance):
        from .commercial import authorize_operation as authorize_commercial

        return authorize_commercial(session, instance, action)
    from .lab import authorize_operation as authorize_historical

    return authorize_historical(session, instance, action)


def advance(session, instance, publish):
    if instance_uses_managed_contract(instance):
        from .commercial import advance as advance_commercial

        return advance_commercial(session, instance, publish)
    from .lab import advance as advance_historical

    return advance_historical(session, instance, publish)


def advance_deletion(session, instance, operation_id, publish):
    if instance_uses_managed_contract(instance):
        from .commercial import advance_deletion as advance_commercial_deletion

        return advance_commercial_deletion(session, instance, operation_id, publish)
    from .lab import advance_deletion as advance_historical_deletion

    return advance_historical_deletion(session, instance, operation_id, publish)


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
        if instance is None or not (
            instance.self_service_policy_id or instance_uses_managed_contract(instance)
        ):
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
