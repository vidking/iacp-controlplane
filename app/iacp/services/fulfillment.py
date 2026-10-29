"""Optional continuation through existing Lifecycle admission and one-shot fences."""
import os
from sqlalchemy import select

from ..models import GuestInitialization, Instance, Operation, TerraformExecution
from ..security import Principal
from .instances import Conflict, event


def advance(instance_id, sessions, publish=None, client_factory=None):
    from ..api.dependencies import get_power_client, get_power_publisher
    from ..lifecycle.service import request_power
    with sessions() as session:
        instance = session.get(Instance, instance_id)
        if not instance or not instance.start_on_create or instance.status != 'ready':
            return
        creation = session.scalar(select(Operation).where(Operation.instance_id == instance_id, Operation.kind == 'instance.create'))
        try:
            if os.environ.get('IACP_ADMISSION_ENABLED', '1') != '1':
                raise Conflict('ADMISSION_DRAINED')
            result = request_power(session, instance, Principal(instance.tenant_id, creation.requested_by, 'operator'),
                'start', 'autostart-' + instance.id, publish or get_power_publisher(), client_factory or get_power_client())
            if result.status == 'completed':
                from .network import after_power
                from .access import after_initialization
                after_power(result.operation_id, sessions)
                with sessions() as observer:
                    initialization = observer.scalar(select(GuestInitialization).where(GuestInitialization.instance_id == instance_id))
                    identifier = initialization.operation_id if initialization and initialization.status == 'completed' else None
                if identifier:
                    after_initialization(identifier, sessions)
        except Exception:
            session.rollback()
            # No provider exception, body or secret enters the audit trail.
            creation = session.get(Operation, creation.id)
            if not session.scalar(select(Operation.id).where(Operation.instance_id == instance_id,
                    Operation.idempotency_key == 'autostart-' + instance_id)):
                event(session, creation, 'autostart_paused', 'worker:iac')
                session.commit()


def after_apply(identifier, sessions):
    from ..models import TerraformApply
    from ..api.dependencies import get_autostart_publisher
    with sessions() as session:
        approval = session.get(TerraformApply, identifier)
        instance_id = approval.instance_id if approval and approval.status == 'completed' else None
    if instance_id:
        with sessions() as session:
            instance = session.get(Instance, instance_id)
            if not instance.start_on_create:
                return
            try:
                get_autostart_publisher()(instance_id)
            except Exception:
                creation = session.scalar(select(Operation).where(Operation.instance_id == instance_id, Operation.kind == 'instance.create'))
                event(session, creation, 'autostart_dispatch_unconfirmed', 'worker:iac')
                session.commit()


def progress(session, instance, provisioning_status):
    if not instance or not instance.start_on_create:
        return None
    start = session.scalar(select(Operation).where(Operation.tenant_id == instance.tenant_id,
        Operation.instance_id == instance.id, Operation.idempotency_key == 'autostart-' + instance.id))
    initialization = session.scalar(select(GuestInitialization).where(GuestInitialization.instance_id == instance.id))
    requires_identity = False
    try:
        from .authorization import member

        technical = member(session, instance, 'start')
        requires_identity = bool(
            technical.profile_snapshots.get(instance.network_profile, {}).get('guest_initialization')
        )
    except Conflict:
        # Before the first plan there is no ownership row yet. The worker will
        # expose the actual provisioning error rather than inventing a guest
        # initialization state.
        requires_identity = False
    phase, status = 'provisioning', provisioning_status
    if provisioning_status == 'completed':
        phase, status = 'start', start.status if start else 'queued'
        if start and start.status == 'completed' and requires_identity:
            phase, status = 'guest_initialization', initialization.status if initialization else 'queued'
        from ..models import CommercialIntent
        from .guest_identity import technical_program_for_mode
        technical_initialization = bool(
            initialization and initialization.program_sha256 == technical_program_for_mode(initialization.mode)[1]
        )
        if status == 'completed' and technical_initialization:
            access = instance.access_verification or {}
            phase, status = 'technical_access', (
                'completed' if access.get('status') == 'completed' else 'queued')
        elif status=='completed' and session.get(CommercialIntent, instance.id) is not None:
            intent=session.get(CommercialIntent,instance.id)
            phase,status='root_bootstrap',intent.bootstrap_status if intent else 'failed'
            if status=='pending':status='queued'
        if status == 'completed' and instance.initial_access_ciphertext:
            access = session.scalar(select(Operation).where(Operation.instance_id == instance.id,
                Operation.idempotency_key == 'access-verify-' + instance.id))
            phase, status = 'access_verification', access.status if access else 'queued'
        if status == 'completed':
            phase = 'verified'
    return {'status': status, 'phase': phase, 'start_operation_id': start.id if start else None,
        'initialization_operation_id': initialization.operation_id if initialization else None,
        'guest_ready': None, 'resume_endpoint': '/v1/instances/' + instance.id + '/provision'}
