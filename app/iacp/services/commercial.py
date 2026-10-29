"""Admisión técnica del contrato único ``provisioning-api-core``.

El nombre del módulo se conserva para no romper imports históricos; el CRM
puede enviar referencias opacas, pero IACP no evalúa pedidos, cuotas ni
políticas comerciales.
"""
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..catalog import catalog_digest, resolve_plan
from ..commercial_contract import resolved_resources
from ..models import CommercialIntent, Instance, Job, Operation, TerraformApply, TerraformExecution, utcnow
from ..schemas import InstanceAccepted
from ..security import Principal
from ..terraform.config import PlanError, instance_executor
from ..terraform.locks import admission_lock
from .bootstrap_secrets import EphemeralSecrets
from .catalog import authorized_profile
from .instances import Conflict, accepted, event
from .jobs import QueueUnavailable
from .contracts import CORE_CONTRACT


RECOVERABLE_BOOTSTRAP_FAILURES = frozenset({
    'BOOTSTRAP_SECRET_EXPIRED_OR_MISSING',
    'BOOTSTRAP_HOST_KEY_CHANNEL_UNAVAILABLE',
    'BOOTSTRAP_HOST_KEY_EVIDENCE_INVALID',
    'BOOTSTRAP_CLOUD_INIT_FAILED',
    'BOOTSTRAP_PREFLIGHT_FAILED',
})


def live_capacity(session,profile,plan):
    """Validate current physical capacity without using historical DB counters.

    ``Instance`` rows are inventory/audit data, not a physical reservation:
    failed, deleted or stale rows may outlive the resource they describe.
    The authoritative admission inputs here are the read-only Proxmox node and
    datastore measurements; the worker revalidates them before apply.
    """
    from .capacity_readonly import CapacityReadOnly
    try:
        client=CapacityReadOnly.configured()
        node=profile.settings.node_name
        storage=profile.settings.boot_disk.datastore_id
        if node not in {'mng01','mng02'} or profile.settings.environment!='lab':raise ValueError()
        n=client._request('GET',f'/nodes/{node}/status')
        s=client._request('GET',f'/nodes/{node}/storage/{storage}/status')
        if not s.get('active') or not s.get('enabled',1):raise ValueError()
        cpus=int(n['cpuinfo']['cpus'])
        cpu_available=cpus*(1-float(n['cpu']))
        if (int(s['avail']) < plan.disk_gb*1024**3
                or int(n['memory']['free']) < plan.memory_mb*1024**2
                or cpu_available<plan.vcpus):
            raise Conflict('COMMERCIAL_PHYSICAL_CAPACITY_EXCEEDED')
    except Conflict:raise
    except Exception:raise Conflict('COMMERCIAL_PHYSICAL_CAPACITY_UNAVAILABLE') from None


def member(session,instance,action,*,current_profiles=False,observation=False):
    from .technical_ownership import member as technical_member

    return technical_member(session, instance, action,
                            current_profiles=current_profiles,
                            observation=observation, adopt=True)


def authorize_operation(session, instance, action):
    """Autoriza una mutación técnica sin cuotas ni políticas comerciales."""
    return member(session, instance, action)


def delegation(session,instance,action,*,current_profiles=False):
    from .technical_ownership import member as technical_member

    return technical_member(session, instance, action,
                            current_profiles=current_profiles, adopt=True)


def restore_expired(session,instance_id,principal,spec,store):
    from ..terraform.locks import instance_lock,ExecutionBusy
    identifier=None
    try:
        with instance_lock(session.get_bind(),instance_id):
            row=session.get(CommercialIntent,instance_id,with_for_update=True,populate_existing=True)
            if (not row or row.bootstrap_status!='failed' or row.bootstrap_started_at
                    or row.error_code not in RECOVERABLE_BOOTSTRAP_FAILURES):return
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
    # No existe un selector de motores: toda nueva KVM entra por Core.
    instance_id=str(uuid4())
    sealed=store.seal(principal.tenant_id,instance_id,spec.access.root_password.get_secret_value())
    values=spec.model_dump(mode='json',exclude={'access'})
    canonical={**values,'access_fingerprint':store.fingerprint(json.dumps({
        'password':spec.access.root_password.get_secret_value(),'keys':spec.access.ssh_public_keys},
        sort_keys=True,separators=(',',':')))}
    omitted = sorted({'network', 'commercial_context'} - spec.model_fields_set)
    if omitted:
        # Huella de la intención recibida, independiente del default resuelto.
        # El algoritmo de cuerpos completos preexistentes permanece exacto.
        for field in omitted:
            canonical.pop(field, None)
        canonical['pilot_defaults_revision'] = 'lab-dhcp-v1'
        canonical['omitted_fields'] = omitted
    digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    query=select(Operation).where(Operation.tenant_id==principal.tenant_id,Operation.idempotency_key==key)
    reference=None
    try:
        with admission_lock(session):
            previous=session.scalar(query)
            if previous:
                response=accepted(session,previous,digest)
                row=session.get(CommercialIntent,previous.instance_id)
                if (row and row.bootstrap_status=='failed' and not row.bootstrap_started_at
                        and row.error_code in RECOVERABLE_BOOTSTRAP_FAILURES):
                    # Mismo tenant, cuerpo y HMAC: solo reentrega cifrada de la
                    # intención original; no publica planificación ni clonación.
                    restore_expired(session,row.instance_id,principal,spec,store)
                return response
            if instance_executor()!='terraform':raise Conflict('PROVISIONING_REAL_EXECUTOR_REQUIRED')
            if spec.network.profile_id!='lab-dhcp':raise Conflict('WAN_STATIC_EXECUTION_NOT_CERTIFIED')
            resources=resolved_resources(spec)
            profile=authorized_profile(principal.tenant_id, spec.image_id)
            if profile is None:raise Conflict('PROVISIONING_PROFILE_NOT_AUTHORIZED')
            if omitted and (profile.settings.environment != 'lab'
                    or profile.settings.network_mode != 'dhcp'):
                raise Conflict('PROVISIONING_DEFAULTS_NOT_AUTHORIZED')
            if profile.guest_initialization!=spec.image_id+'-lab-identity-v1':
                raise Conflict('PROVISIONING_GUEST_INITIALIZATION_REQUIRED')
            if session.scalar(select(Instance.id).where(Instance.tenant_id==principal.tenant_id,Instance.name==spec.name)):
                raise Conflict('PROVISIONING_INSTANCE_NAME_CONFLICT')
            if spec.start_on_create:
                from ..lifecycle.ownership import mutation_revision
                from ..lifecycle.proxmox import PowerError
                try:
                    mutation_revision()
                except PowerError as error:
                    # Una precondición del proveedor no debe filtrarse como
                    # 500: la admisión queda rechazada sin crear intención.
                    raise Conflict(error.code) from None
            revision=CORE_CONTRACT
            instance=Instance(id=instance_id,tenant_id=principal.tenant_id,name=spec.name,
                **resources,network_profile='lab-dhcp',network_mode='dhcp',start_on_create=spec.start_on_create,
                technical_access_reference=sealed[0],
                technical_access_public_keys=list(spec.access.ssh_public_keys),
                commercial_context=spec.commercial_context.model_dump(exclude_none=True) if spec.commercial_context is not None else None,commercial_revision=revision,status='pending',executor='terraform')
            try:
                allocation=reserve_profile(session,instance)
            except PlanError as error:
                raise Conflict(str(error)) from None
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
            row=CommercialIntent(instance_id=instance.id,revision=revision,plan_id=spec.plan_id,
                image_id=spec.image_id,catalog_sha256=catalog_digest(),request_fingerprint=digest,
                specification={**values,**({'pilot_defaults':{'revision':'lab-dhcp-v1','omitted_fields':omitted}} if omitted else {}),'ssh_public_keys':spec.access.ssh_public_keys,'profile_snapshot':profile.model_dump()},
                # Compatibilidad histórica: el registro puede conservar la
                # referencia opaca durante la transición. La ejecución nueva
                # usa exclusivamente los campos técnicos de Instance y nunca
                # crea instance.access.bootstrap.
                secret_reference=reference,bootstrap_status='pending')
            # ``execution_binding`` pertenece al registro CRM histórico. La
            # autorización operativa se materializa después del apply en
            # ``technical_ownership`` y nunca depende de esta fila.
            row.execution_binding=None
            session.add(row);event(session,operation,'provisioning_accepted',principal.subject);session.commit()
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
        from .automation import technical_principal
        automation = technical_principal(instance.tenant_id, CORE_CONTRACT)
        approval=approve(session,execution,{'attempt':execution.attempt,'plan_sha256':execution.plan_sha256,
            'valid_for_seconds':900},automation.subject+':apply:'+instance.id+':'+str(execution.attempt),
            automation)
    return dispatch(session,execution,approval.id,'provisioning-api-core-apply:'+approval.id,principal,publish)


def advance_deletion(session, instance, operation_id, publish):
    """Delega el destroy técnico a la identidad explícita del contrato."""
    from ..models import DeletionExecution
    from ..terraform import deletion

    row = session.get(DeletionExecution, operation_id, populate_existing=True)
    operation = session.get(Operation, operation_id)
    if row is None or not row.autonomous or row.status in {"failed", "completed"}:
        return
    principal = Principal(instance.tenant_id, operation.requested_by, "operator")
    if row.started_at:
        return deletion.recover_or_cancel(session, instance, principal, operation_id, publish)
    member(session, instance, "delete", current_profiles=True)
    body = {"operation_id": row.operation_id, "plan_sha256": row.plan_sha256}
    if row.status == "planned":
        from .automation import technical_principal

        automation = technical_principal(
            instance.tenant_id,
            CORE_CONTRACT,
        )
        deletion.approve(
            session,
            instance,
            automation,
            {**body, "valid_for_seconds": 300},
            f"{automation.subject}:delete:{row.operation_id}",
        )
    if row.phase in {"approved", "delete_queued"}:
        return deletion.dispatch(session, instance, principal, body, operation.idempotency_key, publish)
