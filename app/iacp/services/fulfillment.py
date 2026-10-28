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
    policy = None
    if instance.self_service_policy_id:
        # Solo las instancias históricas consultan la fila LAB que ya poseen.
        from ..models import LabPolicy
        policy = session.get(LabPolicy, instance.self_service_policy_id)
    requires_identity = bool(instance.commercial_revision or policy and policy.profile_snapshots.get(instance.network_profile, {}).get('guest_initialization'))
    phase, status = 'provisioning', provisioning_status
    if provisioning_status == 'completed':
        phase, status = 'start', start.status if start else 'queued'
        if start and start.status == 'completed' and requires_identity:
            phase, status = 'guest_initialization', initialization.status if initialization else 'queued'
        if status=='completed' and instance.commercial_revision:
            from ..models import CommercialIntent
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
