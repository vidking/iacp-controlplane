"""Authenticated asynchronous guest-network discovery; no network/state mutations."""
from uuid import uuid4
import ipaddress

from sqlalchemy import select

from ..models import Instance, Job, NetworkDiscovery, Operation, Tenant, TerraformExecution
from ..lifecycle.certification_binding import binding_digest
from ..lifecycle.service import managed_target
from .instances import Conflict,event
from .jobs import QueueUnavailable
from ..terraform.apply import fingerprint
from ..terraform.locks import ExecutionBusy,instance_lock


def request_discovery(session,instance,principal,key,publish):
    if instance.network_mode!='dhcp':
        raise Conflict('DHCP_PROFILE_REQUIRED')
    digest=fingerprint({'instance_id':instance.id,'action':'network.discover'})
    try:
        with instance_lock(session.get_bind(),instance.id):
            instance=session.get(Instance,instance.id,populate_existing=True)
            previous=session.scalar(select(Operation).where(Operation.tenant_id==principal.tenant_id,Operation.idempotency_key==key))
            if previous:
                if previous.kind!='instance.network.discover' or previous.request_hash!=digest or previous.requested_by!=principal.subject:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                operation=previous
                result=accepted(session,previous)
                publish_needed=previous.status in {'queued','running'}
                session.commit()
            else:
                target,approval=managed_target(session,instance)
                binding=binding_digest(instance,session.get(TerraformExecution,instance.id),approval)
                operation=Operation(id=str(uuid4()),tenant_id=instance.tenant_id,instance_id=instance.id,kind='instance.network.discover',
                                    status='queued',idempotency_key=key,request_hash=digest,requested_by=principal.subject)
                session.add(operation);session.flush()
                session.add(Job(id=str(uuid4()),operation=operation.kind,operation_id=operation.id,tenant_id=instance.tenant_id,status='queued'))
                session.add(NetworkDiscovery(operation_id=operation.id,instance_id=instance.id,provisioning_binding=binding))
                event(session,operation,'network_discovery_accepted',principal.subject);session.commit()
                result=accepted(session,operation);publish_needed=True
    except ExecutionBusy:
        raise Conflict('INSTANCE_BUSY') from None
    if publish_needed:
        try:publish(operation.id)
        except Exception:raise QueueUnavailable from None
    return result


def accepted(session,operation):
    job=session.scalar(select(Job).where(Job.operation_id==operation.id))
    return {'instance_id':operation.instance_id,'operation_id':operation.id,'job_id':job.id,'status':operation.status}


def observe_guest(execution,instance):
    """Use the worker's existing TF token with GuestAgent.Audit, never API secrets."""
    import ssl
    from urllib.request import build_opener,ProxyHandler,HTTPSHandler
    from ..terraform.lifecycle import build_executor
    from ..lifecycle.proxmox import ProxmoxPower,PowerError
    from ..terraform.observation import NoRedirect
    import tempfile
    from pathlib import Path
    expected_mac=(instance.result or {}).get('network',{}).get('mac_address')
    if not expected_mac:
        return {'status':'pending','source':'qemu-guest-agent','ip_addresses':[],'reason':'MAC_NOT_OBSERVED'}
    with tempfile.TemporaryDirectory() as temporary:
        env=build_executor(observation=True).environment(Path(temporary))
        client=ProxmoxPower(env['PROXMOX_VE_ENDPOINT'].rstrip('/'),env['PROXMOX_VE_API_TOKEN'],
            build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=env.get('SSL_CERT_FILE'))),NoRedirect()))
        path=f'/nodes/{instance.node}/qemu/{instance.vmid}'
        try:
            config=client._request('GET',path+'/config?current=1')
            nic=dict(part.split('=',1) for part in config.get('net0','').split(',') if '=' in part)
            expected=execution.allocation
            ip=dict(part.split('=',1) for part in config.get('ipconfig0','').split(',') if '=' in part)
            vlan=expected['vlan_id']
            if (config.get('name')!=instance.name or nic.get('virtio','').lower()!=expected_mac.lower()
                    or nic.get('bridge')!=expected['network_bridge']
                    or (vlan is None and ('tag' in nic or 'trunks' in nic))
                    or (vlan is not None and nic.get('tag')!=str(vlan))
                    or ip.get('ip')!='dhcp' or 'gw' in ip):
                raise Conflict('NETWORK_BINDING_CHANGED')
            result=client._request('GET',path+'/agent/network-get-interfaces')
        except PowerError:
            return {'status':'pending','source':'qemu-guest-agent','ip_addresses':[],'reason':'GUEST_AGENT_UNAVAILABLE'}
    return agent_addresses(result,expected_mac)


def agent_addresses(result,expected_mac):
    rows=result.get('result',[]) if isinstance(result,dict) else result
    if not isinstance(rows,list):rows=[]
    addresses=[]
    for interface in rows:
        if interface.get('hardware-address','').lower()!=expected_mac.lower():continue
        for entry in interface.get('ip-addresses',[]):
            try:address=ipaddress.ip_address(entry['ip-address'])
            except (ValueError,KeyError):continue
            if address.version==4 and not (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified):
                addresses.append(str(address))
    return {'status':'observed' if addresses else 'pending','source':'qemu-guest-agent','ip_addresses':sorted(set(addresses)),
            'mac_address':expected_mac}


def execute_discovery(identifier,sessions,observer=observe_guest):
    from ..models import TerraformExecution,utcnow
    with sessions() as s:
        row=s.get(NetworkDiscovery,identifier)
        if row is None:return
        instance_id=row.instance_id
    try:
        with instance_lock(sessions.kw['bind'],instance_id) as heartbeat:
            with sessions() as s:
                operation=s.get(Operation,identifier,with_for_update=True)
                if operation.status not in {'queued','running'}:return
                row=s.get(NetworkDiscovery,identifier);instance=s.get(Instance,instance_id)
                job=s.scalar(select(Job).where(Job.operation_id==identifier))
                operation.status=job.status='running';s.commit()
                try:
                    tenant=s.get(Tenant,instance.tenant_id)
                    if (not tenant or not tenant.active or operation.tenant_id!=instance.tenant_id or job.tenant_id!=instance.tenant_id
                            or instance.network_mode!='dhcp' or operation.kind!='instance.network.discover' or job.operation!=operation.kind):
                        raise Conflict('TENANT_OR_NETWORK_BINDING_CHANGED')
                    target,approval=managed_target(s,instance);execution=s.get(TerraformExecution,instance.id)
                    if row.provisioning_binding!=binding_digest(instance,execution,approval):raise Conflict('NETWORK_BINDING_CHANGED')
                    from ..models import GuestInitialization,LabPolicy
                    policy=s.get(LabPolicy,instance.self_service_policy_id) if instance.self_service_policy_id else None
                    requires_identity=bool(policy and policy.profile_snapshots.get(instance.network_profile,{}).get('guest_initialization'))
                    initialization=s.scalar(select(GuestInitialization).where(GuestInitialization.instance_id==instance.id))
                    if requires_identity and (not initialization or initialization.status!='completed'):
                        result={'status':'pending','source':'qemu-guest-agent','ip_addresses':[],'reason':'GUEST_INITIALIZATION_REQUIRED'}
                    else:result=observer(execution,instance)
                    if result.get('ip_addresses'):
                        others=s.scalars(select(Instance).where(Instance.id!=instance.id,Instance.status!='deleted',
                            Instance.network_mode=='dhcp')).all()
                        if any(set(result['ip_addresses']) & set(other.ip_addresses or []) for other in others):
                            result={**result,'status':'pending','ip_addresses':[],'reason':'DHCP_ADDRESS_CONFLICT'}
                    heartbeat()
                    s.refresh(instance)
                    if instance.status!='ready':raise Conflict('NETWORK_BINDING_CHANGED')
                    result={**result,'observed_at':utcnow().isoformat()}
                    row.result=instance.network_observation=result
                    instance.ip_addresses=result['ip_addresses']
                    operation.status=job.status='completed'
                    event(s,operation,'network_'+result['status'],'worker:terraform')
                except Exception:
                    operation.status=job.status='failed';operation.error_code='NETWORK_DISCOVERY_FAILED'
                    event(s,operation,'network_discovery_failed','worker:terraform')
                s.commit()
    except ExecutionBusy:return


def after_power(identifier,sessions):
    """Called after the power task releases its lock; discovery cannot fail power."""
    from ..security import Principal
    try:
        with sessions() as s:
            operation=s.get(Operation,identifier)
            if not operation or operation.status!='completed' or operation.kind not in {'instance.power.start','instance.power.reboot'}:return
            instance=s.get(Instance,operation.instance_id)
            if instance.network_mode!='dhcp':return
            from ..models import LabPolicy
            policy=s.get(LabPolicy,instance.self_service_policy_id) if instance.self_service_policy_id else None
            if policy and policy.profile_snapshots.get(instance.network_profile,{}).get('guest_initialization'):
                from .guest_identity import request_initialization
                from ..api.dependencies import get_guest_initialization_publisher
                from ..models import GuestInitialization
                initialization=s.scalar(select(GuestInitialization).where(GuestInitialization.instance_id==instance.id))
                if initialization is None:
                    request_initialization(s,instance,Principal(instance.tenant_id,operation.requested_by,'operator'),
                        'identity-'+instance.id,get_guest_initialization_publisher())
                    return
                if initialization.status!='completed':
                    get_guest_initialization_publisher()(initialization.operation_id)
                    return
            from ..api.dependencies import get_network_publisher
            request_discovery(s,instance,Principal(instance.tenant_id,operation.requested_by,'operator'),
                              'network-after-'+identifier,get_network_publisher())
    except Exception:
        # Caller can retry discovery via the authenticated API; never retry power.
        return
