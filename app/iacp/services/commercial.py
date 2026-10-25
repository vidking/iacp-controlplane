"""Admisión comercial automática; separa catálogo, cuotas, capacidad y ejecución."""
from datetime import timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..catalog import CATALOG_VERSION, catalog_digest, resolve_plan
from ..commercial_contract import resolved_resources
from ..models import CommercialIntent, Instance, Job, Operation, TerraformApply, TerraformExecution, utcnow
from ..schemas import InstanceAccepted
from ..security import Principal
from ..terraform.config import PlanError, SOURCE_REVISION, instance_executor
from ..terraform.locks import admission_lock
from .bootstrap_secrets import EphemeralSecrets
from .catalog import authorized_profile
from .instances import Conflict, accepted, event
from .jobs import QueueUnavailable
from .lab import usage


class Limits(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    allowed_plans: list[str] = Field(min_length=1,max_length=4)
    max_instances: int = Field(ge=1)
    max_vcpus: int = Field(ge=1)
    max_memory_mb: int = Field(ge=512)
    max_disk_gb: int = Field(ge=50)
    max_concurrent_operations: int = Field(ge=1,le=2)


def limits_for(tenant_id):
    try:
        path=os.environ.get('IACP_COMMERCIAL_LIMITS_FILE') or os.environ['IACP_TERRAFORM_PROFILES_FILE']
        raw=json.loads(Path(path).read_text())
        limits=Limits.model_validate((raw.get('tenants') or raw.get('commercial_limits',{}))[tenant_id])
        for plan in limits.allowed_plans:resolve_plan(plan)
        return limits
    except (KeyError,ValueError,OSError,TypeError):
        raise Conflict('COMMERCIAL_TENANT_NOT_AUTHORIZED') from None


def live_capacity(session,profile,plan):
    """Lectura física con la identidad existente; sin privilegios nuevos ni IPAM."""
    from .capacity_readonly import CapacityReadOnly
    from ..models import Instance
    try:
        client=CapacityReadOnly.configured()
        node=profile.settings.node_name
        storage=profile.settings.boot_disk.datastore_id
        if node not in {'mng01','mng02'} or profile.settings.environment!='lab':raise ValueError()
        n=client._request('GET',f'/nodes/{node}/status')
        s=client._request('GET',f'/nodes/{node}/storage/{storage}/status')
        if not s.get('active') or not s.get('enabled',1):raise ValueError()
        if s.get('type') in {'rbd','cephfs'}:
            health=client._request('GET',f'/nodes/{node}/ceph/status')
            if health.get('health',{}).get('status')!='HEALTH_OK':raise ValueError()
        pending=session.scalars(select(Instance).where(Instance.status!='deleted')).all()
        # Presupuesto completo, incluso discos thin de ready. Puede descontar
        # otra vez uso físico ya materializado: conservador, nunca sobreventa.
        disk_reserved=sum(i.disk_gb for i in pending)*1024**3
        memory_reserved=sum(i.memory_mb for i in pending)*1024**2
        cpus=int(n['cpuinfo']['cpus'])
        cpu_available=cpus*(1-float(n['cpu']))-sum(i.vcpus for i in pending)
        if (int(s['avail'])-disk_reserved < plan.disk_gb*1024**3
                or int(n['memory']['free'])-memory_reserved < plan.memory_mb*1024**2
                or cpu_available<plan.vcpus):
            raise Conflict('COMMERCIAL_PHYSICAL_CAPACITY_EXCEEDED')
    except Conflict:raise
    except Exception:raise Conflict('COMMERCIAL_PHYSICAL_CAPACITY_UNAVAILABLE') from None


def record_for(session,instance):
    return session.get(CommercialIntent,instance.id)


def binding(instance,execution,row):
    from ..terraform.apply import fingerprint
    return fingerprint({'instance':{k:getattr(instance,k) for k in ['id','tenant_id','name','image','vcpus',
        'memory_mb','disk_gb','network_profile','start_on_create','commercial_context']},
        'allocation':execution.allocation,'state_key':execution.state_key,'source_revision':execution.source_revision,
        'catalog':row.catalog_sha256,'revision':row.revision,'request':row.request_fingerprint,
        'plan_id':row.plan_id,'image_id':row.image_id,'specification':row.specification})


def member(session,instance,action,*,current_profiles=False,observation=False):
    from ..models import Tenant
    row=record_for(session,instance)
    execution=session.get(TerraformExecution,instance.id)
    tenant=session.get(Tenant,instance.tenant_id)
    if (not row or not tenant or not tenant.active or action not in {'create','start','shutdown','reboot','delete'}
            or not execution or execution.source_revision!=SOURCE_REVISION
            or execution.vmid is None or execution.vmid<30000 or execution.vmid in {104,106,29000}
            or row.execution_binding!=binding(instance,execution,row)):
        raise Conflict('COMMERCIAL_OWNERSHIP_BINDING_CHANGED')
    if observation:return row
    limits=limits_for(instance.tenant_id)
    if row.plan_id not in limits.allowed_plans:raise Conflict('COMMERCIAL_PLAN_NOT_AUTHORIZED')
    if current_profiles:
        current=authorized_profile(instance.tenant_id)
        if current is None or current.model_dump()!=row.specification['profile_snapshot']:
            raise Conflict('COMMERCIAL_PROFILE_CHANGED')
    return row


def delegation(session,instance,action,*,current_profiles=False):
    row=member(session,instance,action,current_profiles=current_profiles)
    snapshot=dict(row.specification['profile_snapshot'])
    if snapshot.get('guest_initialization'):
        from .guest_identity import PROGRAM_SHA256
        snapshot['guest_initialization_program_sha256']=PROGRAM_SHA256
    return SimpleNamespace(id='commercial:'+row.catalog_sha256,
        profile_snapshots={instance.network_profile:snapshot},
        specification={'actions':['create','start','shutdown','reboot','delete']})


def quota(session,tenant_id,limits,resources=None):
    used=usage(session,tenant_id)
    additions={'instances':1 if resources else 0,'vcpus':resources['vcpus'] if resources else 0,
        'memory_mb':resources['memory_mb'] if resources else 0,'disk_gb':resources['disk_gb'] if resources else 0,
        'concurrent_operations':1}
    for name in additions:
        if used[name]+additions[name]>getattr(limits,'max_'+name):
            raise Conflict('COMMERCIAL_QUOTA_EXCEEDED:'+name)


def restore_expired(session,instance_id,principal,spec,store):
    from ..terraform.locks import instance_lock,ExecutionBusy
    identifier=None
    try:
        with instance_lock(session.get_bind(),instance_id):
            row=session.get(CommercialIntent,instance_id,with_for_update=True,populate_existing=True)
            if not row or row.bootstrap_status!='failed' or row.bootstrap_started_at or row.error_code!='BOOTSTRAP_SECRET_EXPIRED_OR_MISSING':return
            instance=session.get(Instance,instance_id,populate_existing=True)
            member(session,instance,'start',current_profiles=True)
            store.restore(row.secret_reference,principal.tenant_id,row.instance_id,spec.access.root_password.get_secret_value())
            row.bootstrap_status='pending';row.error_code=None
            bootstrap=session.scalar(select(Operation).where(Operation.instance_id==row.instance_id,
                Operation.kind=='instance.access.bootstrap'))
            if bootstrap:
                bootstrap.status='queued';bootstrap.error_code=None
                session.scalar(select(Job).where(Job.operation_id==bootstrap.id)).status='queued'
                identifier=bootstrap.id
            session.commit()
    except ExecutionBusy:raise Conflict('COMMERCIAL_BOOTSTRAP_ACTIVE') from None
    if identifier:
        from ..api.dependencies import get_commercial_bootstrap_publisher
        try:get_commercial_bootstrap_publisher()(identifier)
        except Exception:raise QueueUnavailable from None


def create(session,principal,spec,key,publish,store=None,capacity=None):
    from ..terraform.profiles import reserve_profile
    from ..terraform.lifecycle import initial_execution
    store=store or EphemeralSecrets.configured()
    capacity=capacity or live_capacity
    instance_id=str(uuid4())
    sealed=store.seal(principal.tenant_id,instance_id,spec.access.root_password.get_secret_value())
    values=spec.model_dump(mode='json',exclude={'access'})
    canonical={**values,'access_fingerprint':store.fingerprint(json.dumps({
        'password':spec.access.root_password.get_secret_value(),'keys':spec.access.ssh_public_keys},
        sort_keys=True,separators=(',',':')))}
    digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    query=select(Operation).where(Operation.tenant_id==principal.tenant_id,Operation.idempotency_key==key)
    reference=None
    try:
        with admission_lock(session):
            previous=session.scalar(query)
            if previous:
                response=accepted(session,previous,digest)
                row=session.get(CommercialIntent,previous.instance_id)
                if row and row.bootstrap_status=='failed' and not row.bootstrap_started_at and row.error_code=='BOOTSTRAP_SECRET_EXPIRED_OR_MISSING':
                    # Mismo tenant, cuerpo y HMAC: solo reentrega cifrada de la
                    # intención original; no publica planificación ni clonación.
                    restore_expired(session,row.instance_id,principal,spec,store)
                return response
            if instance_executor()!='terraform':raise Conflict('COMMERCIAL_REAL_EXECUTOR_REQUIRED')
            if spec.network.profile_id!='lab-dhcp':raise Conflict('WAN_STATIC_EXECUTION_NOT_CERTIFIED')
            resources=resolved_resources(spec)
            limits=limits_for(principal.tenant_id)
            if spec.plan_id not in limits.allowed_plans:raise Conflict('COMMERCIAL_PLAN_NOT_AUTHORIZED')
            profile=authorized_profile(principal.tenant_id)
            if profile is None:raise Conflict('COMMERCIAL_PROFILE_NOT_AUTHORIZED')
            if profile.root_password_bootstrap:raise Conflict('COMMERCIAL_LEGACY_PASSWORD_PROFILE_FORBIDDEN')
            if profile.guest_initialization!='ubuntu-2404-lab-identity-v1':
                raise Conflict('COMMERCIAL_GUEST_INITIALIZATION_REQUIRED')
            if session.scalar(select(Instance.id).where(Instance.tenant_id==principal.tenant_id,Instance.name==spec.name)):
                raise Conflict('COMMERCIAL_INSTANCE_NAME_CONFLICT')
            if spec.start_on_create:
                from ..lifecycle.ownership import mutation_revision
                mutation_revision()
            quota(session,principal.tenant_id,limits,resources)
            instance=Instance(id=instance_id,tenant_id=principal.tenant_id,name=spec.name,
                **resources,network_profile='lab-dhcp',network_mode='dhcp',start_on_create=spec.start_on_create,
                commercial_context=spec.commercial_context.model_dump(exclude_none=True),commercial_revision='commercial-g',status='pending',executor='terraform')
            allocation=reserve_profile(session,instance)
            from ..terraform.profiles import settings_for
            capacity_profile=profile.model_copy(update={'settings':settings_for(profile,allocation.node_name)})
            capacity(session,capacity_profile,resolve_plan(spec.plan_id))
            operation=Operation(id=str(uuid4()),tenant_id=principal.tenant_id,instance_id=instance.id,
                kind='instance.create',status='queued',idempotency_key=key,request_hash=digest,requested_by=principal.subject)
            job=Job(id=str(uuid4()),operation='instance.plan',status='queued',tenant_id=principal.tenant_id,operation_id=operation.id)
            session.add(instance);session.flush();session.add(operation);session.flush();session.add(job);session.flush()
            initial_execution(session,instance,operation,job,allocation)
            if not store.put_sealed(sealed):raise Conflict('BOOTSTRAP_REFERENCE_COLLISION')
            reference=sealed[0]
            row=CommercialIntent(instance_id=instance.id,revision='commercial-g',plan_id=spec.plan_id,
                image_id=spec.image_id,catalog_sha256=catalog_digest(),request_fingerprint=digest,
                specification={**values,'ssh_public_keys':spec.access.ssh_public_keys,'profile_snapshot':profile.model_dump()},
                secret_reference=reference,bootstrap_status='pending')
            row.execution_binding=binding(instance,session.get(TerraformExecution,instance.id),row)
            session.add(row);event(session,operation,'commercial_accepted',principal.subject);session.commit()
    except Exception:
        session.rollback()
        # Una referencia de una transacción abortada solo contiene ciphertext y
        # caduca aun si Redis no está disponible para su limpieza inmediata.
        if reference:
            try:store.delete(reference)
            except Exception:pass
        raise
    result=InstanceAccepted(instance_id=instance.id,operation_id=operation.id,job_id=job.id,executor='terraform')
    try:publish(job.id)
    except Exception:raise QueueUnavailable from None
    return result


def advance(session,instance,publish):
    from ..terraform.apply import approve,dispatch,recover_apply
    row=member(session,instance,'create',current_profiles=True)
    execution=session.get(TerraformExecution,instance.id)
    operation=session.get(Operation,execution.operation_id)
    principal=Principal(instance.tenant_id,operation.requested_by,'operator')
    approval=session.scalar(select(TerraformApply).where(TerraformApply.instance_id==instance.id,
        TerraformApply.attempt==execution.attempt))
    if approval and approval.status in {'running','uncertain'}:
        return recover_apply(session,execution,approval.id,principal,publish)
    if approval and approval.status=='completed':return approval
    if approval and approval.status=='queued':
        publish(approval.id,'apply')
        return approval
    if not approval:
        approval=approve(session,execution,{'attempt':execution.attempt,'plan_sha256':execution.plan_sha256,
            'valid_for_seconds':900},'commercial:'+instance.id+':'+str(execution.attempt),
            Principal(instance.tenant_id,'commercial:'+CATALOG_VERSION,'approver'))
    return dispatch(session,execution,approval.id,'commercial-apply:'+approval.id,principal,publish)
