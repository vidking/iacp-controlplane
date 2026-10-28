"""Entrega root cercada por instancia; una entrega ambigua solo se observa."""
from uuid import uuid4
from sqlalchemy import select

from ..models import CommercialIntent,GuestInitialization,Instance,Job,Operation,Tenant,utcnow
from ..terraform.locks import instance_lock,ExecutionBusy
from .bootstrap_secrets import EphemeralSecrets,BootstrapSecretError
from .technical_ownership import member as technical_member
from .instances import Conflict,event
from .jobs import QueueUnavailable


def active(session,instance_id):
    return session.scalar(select(Operation.id).where(Operation.instance_id==instance_id,
        Operation.kind=='instance.access.bootstrap',Operation.status.in_(['queued','running','uncertain'])))


def request(session,instance,principal,key,publish):
    from ..lifecycle.service import managed_target,response
    with instance_lock(session.get_bind(),instance.id):
        instance=session.get(Instance,instance.id,populate_existing=True)
        technical_member(session,instance,'start',adopt=True)
        managed_target(session,instance)
        initialization=session.scalar(select(GuestInitialization).where(GuestInitialization.instance_id==instance.id))
        if not initialization or initialization.status!='completed' or not instance.ip_addresses:
            raise Conflict('BOOTSTRAP_GUEST_NOT_READY')
        previous=session.scalar(select(Operation).where(Operation.tenant_id==principal.tenant_id,Operation.idempotency_key==key))
        if previous and (previous.instance_id!=instance.id or previous.kind!='instance.access.bootstrap'):
            raise Conflict('IDEMPOTENCY_CONFLICT')
        operation=previous or session.scalar(select(Operation).where(Operation.instance_id==instance.id,
            Operation.kind=='instance.access.bootstrap'))
        if operation is None:
            operation=Operation(id=str(uuid4()),tenant_id=instance.tenant_id,instance_id=instance.id,
                kind='instance.access.bootstrap',status='queued',idempotency_key=key,
                request_hash=session.get(CommercialIntent,instance.id).request_fingerprint,requested_by=principal.subject)
            session.add(operation);session.flush()
            session.add(Job(id=str(uuid4()),operation=operation.kind,status='queued',tenant_id=instance.tenant_id,operation_id=operation.id))
            event(session,operation,'commercial_bootstrap_accepted',principal.subject)
        session.commit();result=response(session,operation)
    if operation.status!='completed':
        try:publish(operation.id)
        except Exception:raise QueueUnavailable from None
    return result


def execute(identifier,sessions,store=None,delivery=None):
    from ..lifecycle.service import managed_target
    from .secure_bootstrap import SecureSSHBootstrap
    delivery=delivery or SecureSSHBootstrap()
    with sessions() as s:
        op=s.get(Operation,identifier)
        if not op or op.kind!='instance.access.bootstrap':return
        instance_id=op.instance_id
    try:
        with instance_lock(sessions.kw['bind'],instance_id):
            with sessions() as s:
                op=s.get(Operation,identifier,with_for_update=True)
                instance=s.get(Instance,instance_id)
                existing=s.get(CommercialIntent,instance_id)
                consumed=bool(existing and existing.bootstrap_started_at)
                technical_member(s,instance,'start',current_profiles=not consumed,observation=consumed,adopt=True)
                row=existing
                job=s.scalar(select(Job).where(Job.operation_id==identifier))
                target,_=managed_target(s,instance)
                if op.tenant_id!=instance.tenant_id or not job or job.tenant_id!=instance.tenant_id:
                    return
                reference=row.secret_reference
                try:
                    if row.bootstrap_status=='completed':
                        if reference:
                            (store or EphemeralSecrets.configured()).delete(reference)
                            row.secret_reference=None;s.commit()
                        return
                    if row.bootstrap_started_at:
                        # No descifra ni reenvía; ni siquiera necesita que Redis
                        # conserve el secreto para observar el marcador por canal autenticado.
                        result=delivery.observe(instance,target,reference)
                        if not result:raise BootstrapSecretError('BOOTSTRAP_RECONCILIATION_REQUIRED')
                    else:
                        store=store or EphemeralSecrets.configured()
                        password=store.read(reference,instance.tenant_id,instance.id)
                        row.bootstrap_started_at=utcnow();row.bootstrap_status='running'
                        op.status=job.status='running'
                        event(s,op,'commercial_bootstrap_dispatch_consumed','worker:iac');s.commit()
                        try:
                            result=delivery.apply(instance,target,reference,password,
                                row.specification['ssh_public_keys'],row.specification['profile_snapshot']['settings']['cloudinit_username'])
                        finally:
                            password=None
                    if result.get('status')!='completed':raise BootstrapSecretError('BOOTSTRAP_RECONCILIATION_REQUIRED')
                    row.bootstrap_status=op.status=job.status='completed'
                    row.bootstrap_completed_at=utcnow();row.error_code=op.error_code=None
                    instance.access_verification={'status':'completed','cloud_init_complete':True,
                        'ssh_authenticated':None,'observed_at':utcnow().isoformat()}
                    event(s,op,'commercial_bootstrap_completed','worker:iac');s.commit()
                    (store or EphemeralSecrets.configured()).delete(reference);row.secret_reference=None;s.commit()
                except Exception as failure:
                    s.rollback();row=s.get(CommercialIntent,instance_id);op=s.get(Operation,identifier)
                    if row.bootstrap_status=='completed':
                        row.error_code='BOOTSTRAP_SECRET_CLEANUP_PENDING';s.commit();return
                    code=str(failure) if isinstance(failure,BootstrapSecretError) else 'BOOTSTRAP_DELIVERY_UNCONFIRMED'
                    row.bootstrap_status=op.status='uncertain' if row.bootstrap_started_at else 'failed'
                    row.error_code=op.error_code=code
                    s.get(Job,job.id).status='failed'
                    instance=s.get(Instance,instance_id)
                    instance.access_verification={'status':row.bootstrap_status,'error_code':code}
                    event(s,op,'commercial_bootstrap_'+row.bootstrap_status,'worker:iac');s.commit()
    except ExecutionBusy:return
    except Exception:
        with sessions() as s:
            row=s.get(CommercialIntent,instance_id);op=s.get(Operation,identifier)
            if row and op and row.bootstrap_status!='completed':
                row.bootstrap_status=op.status='uncertain' if row.bootstrap_started_at else 'failed'
                row.error_code=op.error_code='BOOTSTRAP_POLICY_OR_TARGET_UNAVAILABLE'
                job=s.scalar(select(Job).where(Job.operation_id==identifier))
                if job:job.status='failed'
                event(s,op,'commercial_bootstrap_rejected','worker:iac');s.commit()


def after_initialization(instance_id,sessions):
    from ..api.dependencies import get_commercial_bootstrap_publisher
    from ..security import Principal
    with sessions() as s:
        instance=s.get(Instance,instance_id)
        if not instance or not s.get(CommercialIntent, instance.id):return
        creation=s.scalar(select(Operation).where(Operation.instance_id==instance.id,Operation.kind=='instance.create'))
        try:request(s,instance,Principal(instance.tenant_id,creation.requested_by,'operator'),
            'bootstrap-'+instance.id,get_commercial_bootstrap_publisher())
        except Exception:
            s.rollback();event(s,s.get(Operation,creation.id),'commercial_bootstrap_paused','worker:iac');s.commit()
